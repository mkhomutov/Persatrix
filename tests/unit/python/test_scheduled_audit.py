"""Hold the weekly dependency audit, and Dependabot's image entries, to what they promise.

The weekly audit read published security advisories for the Rust CLI only,
and Dependabot had no entry for a Dockerfile or a compose file, so a base
image had no update path at all (ISSUE-0218). These tests keep four things
true of `.github/workflows/scheduled-audit.yml`:

- it audits all four ecosystems, each with a tool at an exact version;
- one audit failing does not stop the others, and still fails the job, which
  is what makes the last step open the failure issue;
- the Python audit reads every `.github/python-constraints*.txt`, so a second
  constraints file is audited the week it lands;
- the failure issue names every audit and gives the command that audit ran.

And two of `.github/dependabot.yml`: every directory that holds a Dockerfile
or a compose file has an entry, and Dependabot is kept from the two image
tags that must not move on their own.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from _test_infra import REPO_ROOT, ci_job_steps

AUDIT_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "scheduled-audit.yml"
DEPENDABOT = REPO_ROOT / ".github" / "dependabot.yml"

# Step id -> the words its command must hold. A step whose id starts with
# "audit-" is an audit; these four are the ones the issue asked for.
AUDIT_TOOLS = {
    "audit-rust": "cargo deny check",
    "audit-go": "govulncheck",
    "audit-python": "pip-audit",
    "audit-web": "npm audit",
}

# The file names Dependabot's two image updaters read in a directory, copied
# from dependabot-core (docker/lib/dependabot/docker/file_fetcher.rb and
# docker/lib/dependabot/docker_compose/file_fetcher.rb) as of 2026-11-14.
DOCKERFILE_NAME = re.compile(r"dockerfile|containerfile", re.IGNORECASE)
COMPOSE_NAME = re.compile(r"(docker-)?compose(-[\w]+)?(\.[\w-]+)?\.ya?ml", re.IGNORECASE)
# The names are loose (a test called test_dockerfile_….py matches the first),
# so a file counts only if it also has the line that makes it one.
DOCKERFILE_LINE = re.compile(r"^FROM[ \t]+\S", re.MULTILINE)
COMPOSE_LINE = re.compile(r"^services:", re.MULTILINE)


def _steps() -> list[dict[str, Any]]:
    workflow = yaml.safe_load(AUDIT_WORKFLOW.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    assert list(jobs) == ["dependency-audit"], (
        "the audit is one job, so one failure issue covers it"
    )
    return list(jobs["dependency-audit"]["steps"])


def _audit_steps() -> dict[str, dict[str, Any]]:
    return {s["id"]: s for s in _steps() if str(s.get("id", "")).startswith("audit-")}


def _issue_step() -> dict[str, Any]:
    last = _steps()[-1]
    assert str(last.get("uses", "")).startswith("actions/github-script@"), (
        "the last step is the one that opens the failure issue"
    )
    return last


def _command(step: dict[str, Any], tool: str) -> str:
    """The one line of a step's script that runs its audit tool, without shell plumbing."""
    lines = [
        ln.strip()
        for ln in str(step["run"]).splitlines()
        if tool in ln and not ln.strip().startswith("echo")
    ]
    assert len(lines) == 1, (
        f"step {step['id']!r} should run {tool!r} on exactly one line, found {lines}"
    )
    return lines[0].split(" || ")[0]


def test_the_weekly_audit_reads_all_four_ecosystems() -> None:
    audits = _audit_steps()
    for step_id, tool in AUDIT_TOOLS.items():
        assert step_id in audits, f"the weekly audit has no {step_id!r} step"
        assert tool in audits[step_id]["run"], f"step {step_id!r} does not run {tool!r}"


def test_each_audit_tool_is_pinned_to_an_exact_version() -> None:
    audits = _audit_steps()
    text = AUDIT_WORKFLOW.read_text(encoding="utf-8")
    exact = r"\d+\.\d+\.\d+"
    assert re.search(rf"cargo install cargo-deny --locked --version {exact}\s", text)
    assert re.search(rf"govulncheck@v{exact}\s", audits["audit-go"]["run"] + "\n")
    assert re.search(rf"pip-audit=={exact}\s", audits["audit-python"]["run"])
    # npm comes with Node, so the Node release is npm's pin. It is the release
    # CI's web-console job installs, which test_web_node_engines_floor.py
    # holds to the lowest one web/package.json admits.
    node = [s for s in _steps() if str(s.get("uses", "")).startswith("actions/setup-node@")]
    assert len(node) == 1
    ci_node = [
        s
        for s in ci_job_steps("web-console")
        if str(s.get("uses", "")).startswith("actions/setup-node@")
    ]
    assert str(node[0]["with"]["node-version"]) == str(ci_node[0]["with"]["node-version"])
    assert re.fullmatch(exact, str(node[0]["with"]["node-version"]))


def test_one_failing_audit_does_not_stop_the_others_and_still_fails_the_job() -> None:
    steps = _steps()
    assert str(steps[0].get("uses", "")).startswith("actions/checkout@")
    for step in steps[1:-1]:
        label = step.get("name") or step.get("uses")
        # A step with no `if`, or one that names no status function, runs only
        # while every step before it passed.
        assert "!cancelled()" in str(step.get("if", "")), (
            f"step {label!r} is skipped once an earlier audit fails"
        )
    for step in steps:
        label = step.get("name") or step.get("uses")
        # continue-on-error would let the job pass with a failed audit, and
        # the issue step below runs only when the job has failed.
        assert "continue-on-error" not in step, f"step {label!r} would not fail the job"
    assert _issue_step().get("if") == "failure()"


def _read_go_line(folder: Path) -> subprocess.CompletedProcess[str]:
    """Run the step that picks the audit's Go line in `folder`; it writes to `folder/output`."""
    (reader,) = [s for s in _steps() if s.get("id") == "go-line"]
    (folder / "output").touch()
    return subprocess.run(
        ["bash", "-e", "-c", reader["run"]],
        cwd=folder,
        env={"PATH": os.environ["PATH"], "GITHUB_OUTPUT": str(folder / "output")},
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_go_audit_runs_on_the_go_line_the_orchestrator_image_builds_with(
    tmp_path: Path,
) -> None:
    # In a copy of the real Dockerfile, the image's Go line is moved to one
    # go.mod does not name, so the step shows which of the two it reads.
    dockerfile = (REPO_ROOT / "Dockerfile.orchestrator").read_text(encoding="utf-8")
    moved, count = re.subn(r"(?m)^FROM golang:\d+\.\d+", "FROM golang:9.87", dockerfile)
    assert count == 1, "Dockerfile.orchestrator should have one `FROM golang:<major>.<minor>` line"
    (tmp_path / "Dockerfile.orchestrator").write_text(moved, encoding="utf-8")
    (tmp_path / "go.mod").write_text(
        (REPO_ROOT / "go.mod").read_text(encoding="utf-8"), encoding="utf-8"
    )
    done = _read_go_line(tmp_path)
    assert done.returncode == 0, done.stderr
    assert (tmp_path / "output").read_text(encoding="utf-8") == "line=9.87\n"

    steps = _steps()
    (reader,) = [s for s in steps if s.get("id") == "go-line"]
    (setup,) = [s for s in steps if str(s.get("uses", "")).startswith("actions/setup-go@")]
    # A two-part version takes the newest patch release of that line, and
    # check-latest looks for it instead of settling for a cached older one.
    assert setup["with"] == {
        "go-version": "${{ steps.go-line.outputs.line }}",
        "check-latest": True,
    }
    assert steps.index(reader) < steps.index(setup) < steps.index(_audit_steps()["audit-go"])


def test_the_go_line_step_fails_when_the_dockerfile_names_no_go_line(tmp_path: Path) -> None:
    # An empty answer would let setup-go fall back to whatever Go the runner has.
    (tmp_path / "Dockerfile.orchestrator").write_text("FROM alpine:3.20\n", encoding="utf-8")
    done = _read_go_line(tmp_path)
    assert done.returncode != 0
    assert (tmp_path / "output").read_text(encoding="utf-8") == ""


def _run_python_audit(tmp_path: Path, files: list[str], failing: set[str]) -> tuple[int, list[str]]:
    """Run the Python audit step's script with a stand-in `uv`.

    Returns the script's exit status and the files the stand-in was given.
    """
    (tmp_path / ".github").mkdir()
    for name in files:
        (tmp_path / ".github" / name).write_text("example==1.0.0\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    seen = tmp_path / "seen.txt"
    fake_uv = bin_dir / "uv"
    # The stand-in records the file it was given (the last argument) and fails
    # for the ones named in FAILING, as pip-audit does on a finding.
    fake_uv.write_text(
        '#!/bin/sh\nfor last; do :; done\necho "$last" >> "$SEEN"\n'
        'case " $FAILING " in *" $last "*) exit 1;; esac\nexit 0\n',
        encoding="utf-8",
    )
    fake_uv.chmod(fake_uv.stat().st_mode | stat.S_IXUSR)
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "SEEN": str(seen),
        "FAILING": " ".join(f".github/{name}" for name in failing),
    }
    # GitHub runs a `run:` script with `bash -e`.
    done = subprocess.run(
        ["bash", "-e", "-c", _audit_steps()["audit-python"]["run"]],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    read = seen.read_text(encoding="utf-8").split() if seen.exists() else []
    return done.returncode, read


def test_the_python_audit_reads_every_constraints_file(tmp_path: Path) -> None:
    files = ["python-constraints.txt", "python-constraints-providers.txt"]
    status, read = _run_python_audit(tmp_path, files, failing=set())
    assert status == 0
    assert sorted(read) == sorted(f".github/{name}" for name in files)


@pytest.mark.parametrize("failing", ["python-constraints.txt", "python-constraints-providers.txt"])
def test_a_finding_in_one_constraints_file_fails_the_step_after_both_are_read(
    tmp_path: Path, failing: str
) -> None:
    files = ["python-constraints.txt", "python-constraints-providers.txt"]
    status, read = _run_python_audit(tmp_path, files, failing={failing})
    assert status != 0, "a finding must fail the step"
    assert sorted(read) == sorted(f".github/{name}" for name in files), (
        "the other file was not audited"
    )


def test_the_python_audit_resolves_nothing_and_runs_on_ci_python() -> None:
    command = _command(_audit_steps()["audit-python"], "pip-audit")
    # --no-deps audits the pins as written; --disable-pip builds no environment.
    assert "--no-deps" in command and "--disable-pip" in command
    ci_python = [
        s
        for s in ci_job_steps("python")
        if str(s.get("uses", "")).startswith("actions/setup-python@")
    ]
    assert f"--python {ci_python[0]['with']['python-version']} " in command


def test_the_failure_issue_names_every_audit_and_the_command_it_ran() -> None:
    issue = _issue_step()
    script = str(issue["with"]["script"])
    passed_in = " ".join(str(v) for v in issue.get("env", {}).values())
    audits = _audit_steps()
    assert set(AUDIT_TOOLS) <= set(audits)
    for step_id, step in audits.items():
        # Every audit step, the four above and any added later.
        assert f"steps.{step_id}.outcome" in passed_in, (
            f"the issue is not told how {step_id!r} ended"
        )
        tool = AUDIT_TOOLS.get(step_id, "")
        command = _command(step, tool) if tool else str(step["run"]).strip()
        where = step.get("working-directory")
        local = f"cd {where} && {command}" if where else command
        assert local in script, f"the issue does not give {step_id!r}'s command: {local}"
    assert ".github/python-constraints*.txt" in script


def _tracked_files() -> list[str]:
    done = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    )
    return done.stdout.splitlines()


def _dependabot_entries(ecosystem: str) -> list[dict[str, Any]]:
    config = yaml.safe_load(DEPENDABOT.read_text(encoding="utf-8"))
    return [u for u in config["updates"] if u["package-ecosystem"] == ecosystem]


def _directories(entries: list[dict[str, Any]]) -> set[str]:
    return {str(e["directory"]).strip("/") for e in entries}


@pytest.mark.parametrize(
    ("ecosystem", "name_re", "line_re"),
    [("docker", DOCKERFILE_NAME, DOCKERFILE_LINE), ("docker-compose", COMPOSE_NAME, COMPOSE_LINE)],
    ids=["docker", "docker-compose"],
)
def test_dependabot_reads_every_directory_that_holds_an_image_reference(
    ecosystem: str, name_re: re.Pattern[str], line_re: re.Pattern[str]
) -> None:
    holding = {
        str(Path(f).parent).strip(".")
        for f in _tracked_files()
        if name_re.search(Path(f).name)
        and line_re.search((REPO_ROOT / f).read_text(encoding="utf-8"))
    }
    assert holding, f"no tracked file matches the {ecosystem} updater's file names"
    missing = holding - _directories(_dependabot_entries(ecosystem))
    assert not missing, (
        f"Dependabot has no {ecosystem} entry for: {sorted(d or '/' for d in missing)}"
    )


@pytest.mark.parametrize("ecosystem", ["docker", "docker-compose"])
def test_the_image_entries_follow_the_files_conventions(ecosystem: str) -> None:
    entries = _dependabot_entries(ecosystem)
    assert entries, f"Dependabot has no {ecosystem} entry"
    for entry in entries:
        assert entry["schedule"] == {"interval": "monthly"}
        assert entry["commit-message"]["prefix"] == "chore(deps)"
        assert [g["patterns"] for g in entry["groups"].values()] == [["*"]], (
            "one grouped PR per ecosystem"
        )


def test_dependabot_offers_no_move_of_the_agent_images_python_or_the_node_floor() -> None:
    (entry,) = _dependabot_entries("docker")
    rules = {r["dependency-name"]: r for r in entry["ignore"]}
    # `python:3.12-slim` has no patch part, so minor and major are every move
    # there is; which Python the image runs is an open choice (ISSUE-0219).
    assert set(rules["python"]["update-types"]) == {
        "version-update:semver-major",
        "version-update:semver-minor",
    }
    # A rule with a name alone ignores every version. The Node tag is held to
    # the engines floor by test_web_node_engines_floor.py.
    assert set(rules["node"]) == {"dependency-name"}
