"""Hold the issue the weekly dependency audit opens to what an operator needs from it.

When the audit fails, its last step opens or comments on one issue. The text
is built by a script inside `.github/workflows/scheduled-audit.yml`. These
tests run that script under Node with stand-ins for GitHub, so what they
read is the text an operator would get:

- every audit is listed with how it ended, and an audit that failed comes
  with the whole command its step ran;
- when a set-up step failed, the audit it serves is reported as not run and
  the set-up step is named, with or without a finding elsewhere;
- the advice for a Python finding names a route that moves one package.

`test_scheduled_audit.py` holds the workflow's steps themselves (ISSUE-0218).
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

from ._scheduled_audit_helpers import (
    AUDIT_TOOLS,
    SETUP_FOR,
    SETUP_STEPS,
    audit_steps,
    issue_step,
    local_command,
    outcome_variables,
    walk,
)


def test_the_report_is_told_how_every_audit_and_set_up_step_ended() -> None:
    told = set(outcome_variables())
    assert told == {"checkout", *SETUP_STEPS, *audit_steps()}
    assert set(AUDIT_TOOLS) <= set(audit_steps())


def test_the_issue_gives_each_audit_the_whole_command_its_step_runs() -> None:
    script = str(issue_step()["with"]["script"])
    given = dict(re.findall(r"outcome: process\.env\.(\w+),\s*rerun: '([^']*)',", script))
    variables = outcome_variables()
    for step_id, step in audit_steps().items():
        # Equal, not contained: a step that runs less than the issue says
        # (a level or a check dropped) is as wrong as one that runs more.
        assert given.get(variables[step_id]) == local_command(step), step_id
    assert len(given) == len(audit_steps())


NODE_STAND_INS = """
import { readFileSync } from "node:fs";
const calls = [];
const context = { repo: { owner: "o", repo: "r" }, runId: 7, workflow: "w" };
const open = process.env.OPEN_ISSUE === "1"
  ? [{ title: "Scheduled Dependency Audit Failure", number: 3 }] : [];
const github = { rest: { issues: {
  listForRepo: async () => ({ data: open }),
  createComment: async (args) => { calls.push({ call: "createComment", ...args }); },
  create: async (args) => { calls.push({ call: "create", ...args }); },
} } };
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const script = readFileSync(process.argv[2], "utf8");
await new AsyncFunction("context", "github", script)(context, github);
process.stdout.write(JSON.stringify(calls));
"""


def _issue(tmp_path: Path, outcomes: dict[str, str], open_issue: bool = False) -> dict[str, Any]:
    """Run the report script as the workflow would, and return the one call it makes.

    `outcomes` names the steps that did not succeed; every other step did.
    """
    node = shutil.which("node")
    if node is None:
        if os.environ.get("CI"):
            pytest.fail("this runner has no `node`, so the issue text is not tested anywhere")
        pytest.skip("`node` is not installed")
    (tmp_path / "stand-ins.mjs").write_text(NODE_STAND_INS, encoding="utf-8")
    (tmp_path / "report.js").write_text(str(issue_step()["with"]["script"]), encoding="utf-8")
    variables = outcome_variables()
    env = {name: outcomes.get(step, "success") for step, name in variables.items()}
    done = subprocess.run(
        [node, "stand-ins.mjs", "report.js"],
        cwd=tmp_path,
        env={"PATH": os.environ["PATH"], "OPEN_ISSUE": "1" if open_issue else "0", **env},
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    (call,) = json.loads(done.stdout)
    assert isinstance(call, dict)
    return call


SETUP_NOTE = "Set-up failed"


def test_the_issue_for_a_finding_gives_the_command_and_no_set_up_note(tmp_path: Path) -> None:
    call = _issue(tmp_path, {"audit-web": "failure"})
    body = call["body"]
    assert call["call"] == "create" and call["title"] == "Scheduled Dependency Audit Failure"
    assert "- Web console packages (npm audit): **FAILED**" in body
    assert "- Go modules (govulncheck): passed" in body
    assert local_command(audit_steps()["audit-web"]) in body
    assert SETUP_NOTE not in body
    # Only the audit that failed gets a section.
    assert body.count("\n### ") == 1


@pytest.mark.parametrize("failing", ["checkout", *SETUP_STEPS])
def test_the_issue_for_a_failed_set_up_step_blames_the_set_up(tmp_path: Path, failing: str) -> None:
    """The walk above, read as the operator reads it.

    The audit the step serves is reported as not run, the set-up step is
    named, and no dependency advice is given: nothing was judged.
    """
    body = _issue(tmp_path, {k: v for k, v in walk(failing).items() if v != "success"})["body"]
    assert SETUP_NOTE in body
    assert "**FAILED**" not in body and "\n### " not in body
    stopped = [a for a, setups in SETUP_FOR.items() if failing in setups or failing == "checkout"]
    assert body.count("did not run") >= len(stopped) >= 1
    assert body.count("passed") == len(SETUP_FOR) - len(stopped)


def test_a_finding_and_a_failed_set_up_step_are_both_reported(tmp_path: Path) -> None:
    outcomes = {k: v for k, v in walk("setup-uv").items() if v != "success"}
    body = _issue(tmp_path, {**outcomes, "audit-web": "failure"})["body"]
    # The note used to appear only when no audit had failed, so a finding in
    # one audit hid a set-up failure in another.
    assert SETUP_NOTE in body and "uv" in body.split(SETUP_NOTE, 1)[1].splitlines()[0]
    assert "- Web console packages (npm audit): **FAILED**" in body
    assert "- Python pins (pip-audit): did not run" in body


def test_a_failed_job_with_nothing_to_blame_still_says_something(tmp_path: Path) -> None:
    body = _issue(tmp_path, {})["body"]
    assert "**FAILED**" not in body and SETUP_NOTE not in body
    assert "Read the log" in body


def test_an_open_issue_gets_the_same_text_as_a_comment(tmp_path: Path) -> None:
    call = _issue(tmp_path, {"audit-python": "failure"}, open_issue=True)
    assert call["call"] == "createComment" and call["issue_number"] == 3
    assert "- Python pins (pip-audit): **FAILED**" in call["body"]
    assert local_command(audit_steps()["audit-python"]) in call["body"]


def test_the_python_advice_names_a_route_that_moves_one_package(tmp_path: Path) -> None:
    """`make python-constraints-upgrade` and the weekly refresh are the same
    move of every pin, which is held whenever any check fails on the newest
    releases. Raising one package's floor moves that package and what must
    move with it."""
    body = _issue(tmp_path, {"audit-python": "failure"})["body"]
    advice = body.split("### Python pins (pip-audit)", 1)[1]
    assert "agents/pyproject.toml" in advice
    assert "`make python-constraints`" in advice
    assert advice.index("`make python-constraints`") < advice.index("python-constraints-upgrade")
