"""Hold the weekly image build and offline smoke (ISSUE-0219).

No workflow built a product image: the first step that did was an opt-in
gate of the release sweep. ``.github/workflows/scheduled-image-smoke.yml``
now builds the images from ``main`` every week, checks the agent image
against the pins, and runs the offline demo with a verdict. A scheduled
workflow cannot run before it is on ``main``, so these tests are what holds
it until then, and after:

- it runs on a schedule and by hand, and never on a pull request or a push,
  which is where ``ci.yml`` leaves image builds out on purpose;
- each step that does work is one ``make`` target that exists, so a run on a
  contributor's machine is the same commands;
- one check failing does not stop the other, a failed check fails the job,
  and the stack is stopped however the run went;
- when the job fails, its last step opens or comments on one issue, and the
  text says which check failed and the command to run. The text is built by
  a script inside the workflow; the tests run that script under Node with
  stand-ins for GitHub, so what they read is what an operator would get;

The offline smoke's own verdict is held in ``test_offline_smoke_verdict.py``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from _test_infra import REPO_ROOT, ci_workflow, makefile_recipe_body

WORKFLOW = REPO_ROOT / ".github" / "workflows" / "scheduled-image-smoke.yml"
ISSUE_TITLE = "Scheduled Image Smoke Failure"
#: Step id -> the one command it runs.
CHECKS = {"image-pins": "make image-pins-check", "smoke": "make smoke-offline"}
AFTER = {"smoke-logs": "make smoke-offline-logs", "stop": "make docker-down"}


def _workflow() -> dict[Any, Any]:
    return dict(yaml.safe_load(WORKFLOW.read_text(encoding="utf-8")))


def _steps() -> list[dict[str, Any]]:
    jobs = _workflow()["jobs"]
    assert list(jobs) == ["image-smoke"], "one job, so one failure issue covers it"
    return list(jobs["image-smoke"]["steps"])


def _by_id() -> dict[str, dict[str, Any]]:
    return {str(step["id"]): step for step in _steps() if "id" in step}


def _issue_step() -> dict[str, Any]:
    last = _steps()[-1]
    assert str(last.get("uses", "")).startswith("actions/github-script@")
    return last


# ─── When it runs ─────────────────────────────────────────────────────────


def test_it_runs_weekly_and_by_hand_and_on_no_pull_request() -> None:
    # PyYAML reads the key `on` as the boolean True.
    triggers = _workflow()[True]
    assert set(triggers) == {"schedule", "workflow_dispatch"}, triggers
    (schedule,) = triggers["schedule"]
    minute, hour, day, month, weekday = str(schedule["cron"]).split()
    assert (day, month) == ("*", "*") and weekday in "0123456", "once a week"
    assert minute.isdigit() and hour.isdigit()


_RUNS_DOCKER = re.compile(r"\bdocker\s+(?:compose|build)\b")


def _make_targets() -> dict[str, tuple[list[str], str]]:
    """Each target of the Makefile: its prerequisites and its recipe."""
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    found: dict[str, tuple[list[str], str]] = {}
    for rule in re.finditer(r"^([\w.-]+):(?!=)([^\n]*)\n((?:\t[^\n]*\n)*)", makefile, re.M):
        found[rule.group(1)] = (rule.group(2).split("##", 1)[0].split(), rule.group(3))
    return found


def _targets_that_run_docker() -> set[str]:
    """Every target that runs `docker build` or `docker compose`: in its own
    recipe, in a prerequisite's, or in a target its recipe hands to `$(MAKE)`."""
    targets = _make_targets()

    def runs_docker(name: str, seen: frozenset[str]) -> bool:
        if name in seen or name not in targets:
            return False
        prerequisites, recipe = targets[name]
        if _RUNS_DOCKER.search(recipe):
            return True
        handed_on = re.findall(r"\$\(MAKE\)(?:\s+--?[\w-]+)*\s+([\w.-]+)", recipe)
        return any(runs_docker(t, seen | {name}) for t in (*prerequisites, *handed_on))

    return {name for name in targets if runs_docker(name, frozenset())}


def test_ci_still_builds_no_product_image() -> None:
    """ci.yml's header says full image builds are excluded; that stays true.

    No step runs `docker build` or `docker compose`, and no step runs a make
    target that does, by whatever road: `make docker-build`, a demo, or a
    target that only depends on one. The build-context job is the one place
    CI runs Docker: a two-line probe built from inside a script, which is no
    product image and is not what this test reads."""
    docker_targets = _targets_that_run_docker()
    assert {
        "docker-build", "docker-up", "demo-offline", "demo-autonomous", "demo-autonomous-start",
        "image-pins-check", "smoke-offline",
    } <= docker_targets, "the Makefile was read wrongly: these all run Docker"
    assert "dockerignore-check" not in docker_targets and "test-python" not in docker_targets
    for name, job in ci_workflow()["jobs"].items():
        for step in job["steps"]:
            run = str(step.get("run", ""))
            assert not _RUNS_DOCKER.search(run), (name, run)
            made = {
                word for words in re.findall(r"\bmake\s+([^\n;&|]+)", run) for word in words.split()
            }
            assert not made & docker_targets, (name, run, sorted(made & docker_targets))


def test_the_job_may_read_the_code_and_write_issues_and_nothing_more() -> None:
    job = _workflow()["jobs"]["image-smoke"]
    assert job["permissions"] == {"contents": "read", "issues": "write"}
    assert int(job["timeout-minutes"]) <= 90, "a stuck stack does not hold a runner for six hours"


def test_no_key_reaches_the_run() -> None:
    """"No key, no provider call" is what makes the run free. So the workflow
    reads no secret anywhere, and sets no variable that could be a key, for
    the workflow, the job or any step. The compose files hand a provider's
    key on to every agent when the environment holds one."""
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    code = "\n".join(line for line in lines if not line.lstrip().startswith("#"))
    assert "secrets." not in code, "the weekly image run reads no secret"
    job = _workflow()["jobs"]["image-smoke"]
    names = {
        str(name)
        for scope in (_workflow(), job, *_steps())
        for name in (scope.get("env") or {})
    }
    assert names, "the report step is given the steps' outcomes this way"
    could_be_a_key = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", re.I)
    suspect = sorted(name for name in names if could_be_a_key.search(name))
    assert not suspect, f"a variable that could carry a key is set: {suspect}"


def test_each_check_has_a_time_limit_of_its_own_inside_the_jobs() -> None:
    """A job that reaches its own limit is cancelled, and a cancelled job runs
    neither the logs step nor the report: nobody would hear of it. A step
    that reaches a limit of its own fails as a step, and the report runs. So
    every step that can hang has a limit, and together they fit in the job's."""
    job_limit = int(_workflow()["jobs"]["image-smoke"]["timeout-minutes"])
    steps = _by_id()
    limits = {
        step_id: int(steps[step_id]["timeout-minutes"])
        for step_id in ("image-pins", "setup-rust", "smoke", "smoke-logs", "stop")
    }
    assert all(limit > 0 for limit in limits.values()), limits
    assert sum(limits.values()) < job_limit, (limits, job_limit)


# ─── What it runs ─────────────────────────────────────────────────────────


def test_each_working_step_is_one_make_target_that_exists() -> None:
    steps = _by_id()
    for step_id, command in {**CHECKS, **AFTER}.items():
        assert str(steps[step_id]["run"]).strip() == command, step_id
        makefile_recipe_body(command.split()[-1])  # raises when the Makefile has no such target
    others = [s for s in _steps() if "run" in s and s.get("id") not in {**CHECKS, **AFTER}]
    assert [str(s["run"]).strip() for s in others] == ["rustup toolchain install --no-self-update"]


def _recipe_lines(target: str) -> list[str]:
    """A target's recipe, one command a line, without Make's `@`."""
    return [line.strip().lstrip("@") for line in makefile_recipe_body(target).splitlines()]


def test_the_smoke_target_marks_the_history_before_the_convening_and_judges_after_it() -> None:
    """The order is the point: the stack is up, the mark is taken, and only
    then is the roundtable convened. A mark taken after the convening would
    swallow this run's own messages; none at all lets an earlier run's count."""
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    assert re.search(r"^smoke-offline: demo-autonomous-start( |$)", makefile, re.M), (
        "smoke-offline starts the offline stack first, without convening it"
    )
    start = makefile_recipe_body("demo-autonomous-start")
    assert "docker-compose.offline.yaml up -d --build" in start, (
        "the start builds both product images"
    )
    assert "channel convene" not in start, "nothing is convened before the mark"
    assert _recipe_lines("smoke-offline") == [
        "$(PYTHON) scripts/checks/offline_smoke.py --mark $(SMOKE_MARK)",
        "$(MAKE) --no-print-directory demo-autonomous-convene",
        "$(PYTHON) scripts/checks/offline_smoke.py --since $(SMOKE_MARK)",
    ]
    assert "channel convene group:roundtable" in makefile_recipe_body("demo-autonomous-convene")


def test_the_demo_is_still_the_start_and_then_the_convening() -> None:
    """`make demo-autonomous` runs the same two halves the smoke runs, with
    nothing between them, so the smoke judges the demo and not a copy of it."""
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    assert re.search(r"^demo-autonomous: demo-autonomous-start( |$)", makefile, re.M)
    assert _recipe_lines("demo-autonomous") == [
        "$(MAKE) --no-print-directory demo-autonomous-convene",
    ]


def test_one_failing_check_does_not_stop_the_other_and_still_fails_the_job() -> None:
    steps = _by_id()
    for step_id in CHECKS:
        step = steps[step_id]
        assert "!cancelled()" in str(step["if"]), f"{step_id} would be skipped after a failure"
        assert not step.get("continue-on-error"), f"{step_id} could fail without failing the job"
    assert "steps.image-pins" not in str(steps["smoke"]["if"]), (
        "the smoke does not wait for the pins"
    )
    assert "steps.setup-rust.outcome == 'success'" in str(steps["smoke"]["if"])
    assert "steps.smoke" not in str(steps["image-pins"]["if"])


def test_the_stack_is_stopped_however_the_run_went_and_logs_come_only_after_a_failure() -> None:
    steps = _by_id()
    assert "always()" in str(steps["stop"]["if"])
    assert "steps.smoke.outcome == 'failure'" in str(steps["smoke-logs"]["if"])
    order = [str(step.get("id", "")) for step in _steps()]
    assert order.index("smoke") < order.index("smoke-logs") < order.index("stop")


# ─── The issue it opens ───────────────────────────────────────────────────


def _outcome_variables() -> dict[str, str]:
    """Step id -> the variable the report reads its outcome from."""
    found = {}
    for name, value in _issue_step()["env"].items():
        step = re.fullmatch(r"\$\{\{ steps\.([\w-]+)\.outcome \}\}", str(value))
        assert step, f"{name} is not a step's outcome: {value}"
        found[step.group(1)] = str(name)
    return found


def test_the_report_runs_only_after_a_failure_and_is_told_how_each_step_ended() -> None:
    assert str(_issue_step()["if"]).strip() == "failure()"
    assert set(_outcome_variables()) == {"checkout", "setup-rust", *CHECKS}


def test_the_issue_gives_each_check_the_command_its_step_runs() -> None:
    script = str(_issue_step()["with"]["script"])
    given = dict(re.findall(r"outcome: process\.env\.(\w+),\s*rerun: '([^']*)',", script))
    variables = _outcome_variables()
    assert {variables[step_id]: command for step_id, command in CHECKS.items()} == given


NODE_STAND_INS = """
import { readFileSync } from "node:fs";
const calls = [];
const context = { repo: { owner: "o", repo: "r" }, runId: 7, workflow: "w" };
const open = process.env.OPEN_ISSUE === "1"
  ? [{ title: "Scheduled Image Smoke Failure", number: 3 }] : [];
const github = { rest: { issues: {
  listForRepo: async (args) => {
    calls.push({ call: "listForRepo", ...args }); return { data: open };
  },
  createComment: async (args) => { calls.push({ call: "createComment", ...args }); },
  create: async (args) => { calls.push({ call: "create", ...args }); },
} } };
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const script = readFileSync(process.argv[2], "utf8");
await new AsyncFunction("context", "github", script)(context, github);
process.stdout.write(JSON.stringify(calls));
"""


def _issue(tmp_path: Path, outcomes: dict[str, str], open_issue: bool = False) -> dict[str, Any]:
    """Run the report script as the workflow would; return the one write it makes.

    `outcomes` names the steps that did not succeed; every other step did.
    """
    node = shutil.which("node")
    if node is None:
        if os.environ.get("CI"):
            pytest.fail("this runner has no `node`, so the issue text is not tested anywhere")
        pytest.skip("`node` is not installed")
    (tmp_path / "stand-ins.mjs").write_text(NODE_STAND_INS, encoding="utf-8")
    (tmp_path / "report.js").write_text(str(_issue_step()["with"]["script"]), encoding="utf-8")
    env = {name: outcomes.get(step, "success") for step, name in _outcome_variables().items()}
    done = subprocess.run(
        [node, "stand-ins.mjs", "report.js"],
        cwd=tmp_path,
        env={"PATH": os.environ["PATH"], "OPEN_ISSUE": "1" if open_issue else "0", **env},
        capture_output=True, text=True, check=False, timeout=60,
    )
    assert done.returncode == 0, done.stderr
    listed, written = json.loads(done.stdout)
    assert listed["call"] == "listForRepo" and listed["labels"] == "docker,automated"
    assert isinstance(written, dict)
    return written


def test_the_issue_for_images_off_their_pins_gives_the_command(tmp_path: Path) -> None:
    call = _issue(tmp_path, {"image-pins": "failure"})
    assert call["call"] == "create" and call["title"] == ISSUE_TITLE
    assert call["labels"] == ["docker", "automated"]
    body = str(call["body"])
    assert "- The agent image against the pins: **FAILED**" in body
    assert "- The offline smoke: passed" in body
    assert "```sh\nmake image-pins-check\n```" in body
    assert "make smoke-offline\n" not in body, "no advice for the check that passed"
    assert "Set-up failed" not in body
    assert "[run 7](https://github.com/o/r/actions/runs/7)" in body


def test_the_issue_for_a_failed_smoke_gives_its_command_and_where_the_logs_are(
    tmp_path: Path,
) -> None:
    body = str(_issue(tmp_path, {"smoke": "failure"})["body"])
    assert "- The offline smoke: **FAILED**" in body
    assert "```sh\nmake smoke-offline\n```" in body
    assert "make smoke-offline-logs" in body and "make docker-down" in body


def test_a_smoke_that_could_not_be_set_up_is_reported_as_not_run(tmp_path: Path) -> None:
    body = str(_issue(tmp_path, {"setup-rust": "failure", "smoke": "skipped"})["body"])
    assert "- The offline smoke: did not run" in body
    assert "**Set-up failed: the Rust toolchain.**" in body
    assert "```sh" not in body, "nothing to re-run: no check was judged and failed"


def test_a_failed_job_with_nothing_to_blame_still_says_something(tmp_path: Path) -> None:
    body = str(_issue(tmp_path, {})["body"])
    assert "another step did" in body


def test_an_open_issue_gets_the_same_text_as_a_comment(tmp_path: Path) -> None:
    created = _issue(tmp_path, {"smoke": "failure"})
    comment = _issue(tmp_path, {"smoke": "failure"}, open_issue=True)
    assert comment["call"] == "createComment" and comment["issue_number"] == 3
    assert str(created["body"]) in str(comment["body"])
