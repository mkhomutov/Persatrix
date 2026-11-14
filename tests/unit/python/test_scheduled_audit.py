"""Hold the weekly dependency audit to what it promises.

The weekly audit read published security advisories for the Rust CLI only
(ISSUE-0218). These tests keep five things true of
`.github/workflows/scheduled-audit.yml`:

- it audits all four ecosystems, each with a tool at an exact version;
- one audit failing does not stop the others, and still fails the job, which
  is what makes the last step open the failure issue;
- an audit runs only when its own set-up steps succeeded, so it never runs
  with whatever tool the runner happens to have, and the issue then says the
  audit did not run instead of blaming a dependency;
- the Python audit reads every `.github/python-constraints*.txt`, so a second
  constraints file is audited the week it lands;
- the report step is told how every audit and every set-up step ended.

`test_scheduled_audit_issue.py` holds the text of the issue the report step
opens, and `test_dependabot_image_entries.py` the Dependabot half of the same
issue.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest
from _test_infra import REPO_ROOT, ci_job_steps

from ._scheduled_audit_helpers import (
    AUDIT_TOOLS,
    AUDIT_WORKFLOW,
    SETUP_FOR,
    SETUP_STEPS,
    audit_steps,
    issue_step,
    tool_command,
    walk,
    workflow_steps,
)


def test_the_weekly_audit_reads_all_four_ecosystems() -> None:
    audits = audit_steps()
    for step_id, tool in AUDIT_TOOLS.items():
        assert step_id in audits, f"the weekly audit has no {step_id!r} step"
        assert tool in audits[step_id]["run"], f"step {step_id!r} does not run {tool!r}"


def test_each_audit_tool_is_pinned_to_an_exact_version() -> None:
    audits = audit_steps()
    text = AUDIT_WORKFLOW.read_text(encoding="utf-8")
    exact = r"\d+\.\d+\.\d+"
    assert re.search(rf"cargo install cargo-deny --locked --version {exact}\s", text)
    assert re.search(rf"govulncheck@v{exact}\s", audits["audit-go"]["run"] + "\n")
    assert re.search(rf"pip-audit=={exact}\s", audits["audit-python"]["run"])
    # npm comes with Node, so the Node release is npm's pin. It is the release
    # CI's web-console job installs, which test_web_node_engines_floor.py
    # holds to the lowest one web/package.json admits.
    node = [s for s in workflow_steps() if str(s.get("uses", "")).startswith("actions/setup-node@")]
    assert len(node) == 1
    ci_node = [
        s
        for s in ci_job_steps("web-console")
        if str(s.get("uses", "")).startswith("actions/setup-node@")
    ]
    assert str(node[0]["with"]["node-version"]) == str(ci_node[0]["with"]["node-version"])
    assert re.fullmatch(exact, str(node[0]["with"]["node-version"]))


def test_the_web_audit_counts_development_packages_and_fails_from_moderate() -> None:
    """Both choices are in the command, not only in the comment above it.

    The console has only development dependencies, so `--omit=dev` would
    audit nothing and could never fail. "moderate" is the lowest level of
    any advisory ISSUE-0087 recorded.
    """
    words = tool_command(audit_steps()["audit-web"], "npm audit").split()
    assert not [w for w in words if w.startswith("--omit")], words
    assert not [w for w in words if w.startswith("--include")], words
    assert [w for w in words if w.startswith("--audit-level")] == ["--audit-level=moderate"]


def test_one_failing_audit_does_not_stop_the_others_and_still_fails_the_job() -> None:
    steps = workflow_steps()
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
    assert issue_step().get("if") == "failure()"


# ─── A failed set-up step ───────────────────────────────────


def test_every_set_up_step_belongs_to_an_audit() -> None:
    steps = workflow_steps()
    ids = [s.get("id") for s in steps]
    assert ids[0] == "checkout"
    for audit, setups in SETUP_FOR.items():
        for setup in setups:
            assert setup in ids, f"the workflow has no {setup!r} step"
            assert ids.index(setup) < ids.index(audit), f"{setup!r} must come before {audit!r}"
    # A step added later that installs a tool must be tied to its audit here,
    # or that audit could run without it.
    known = {"checkout", "cache-cargo-deny", *SETUP_STEPS, *SETUP_FOR}
    assert {i for i in ids[:-1] if i is not None} == known
    assert all(s.get("id") for s in steps[:-1]), "every step before the report has an id"


def test_with_nothing_failing_every_audit_runs() -> None:
    for cache_hit in (False, True):
        outcomes = walk(None, cache_hit=cache_hit)
        assert {a: outcomes[a] for a in SETUP_FOR} == dict.fromkeys(SETUP_FOR, "success")
    # On a cache hit cargo-deny is not installed again, and the audit still runs.
    assert walk(None, cache_hit=True)["setup-cargo-deny"] == "skipped"


@pytest.mark.parametrize("failing", SETUP_STEPS)
def test_an_audit_does_not_run_when_its_set_up_step_failed(failing: str) -> None:
    outcomes = walk(failing)
    for audit, setups in SETUP_FOR.items():
        expected = "skipped" if failing in setups else "success"
        assert outcomes[audit] == expected, f"{failing} failed and {audit} ended {outcomes[audit]}"


def test_no_audit_runs_when_the_checkout_failed() -> None:
    outcomes = walk("checkout")
    assert {outcomes[a] for a in SETUP_FOR} == {"skipped"}
    assert {outcomes[s] for s in SETUP_STEPS} == {"skipped"}


def _read_go_line(folder: Path) -> subprocess.CompletedProcess[str]:
    """Run the step that picks the audit's Go line in `folder`; it writes to `folder/output`."""
    (reader,) = [s for s in workflow_steps() if s.get("id") == "go-line"]
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

    steps = workflow_steps()
    (reader,) = [s for s in steps if s.get("id") == "go-line"]
    (setup,) = [s for s in steps if str(s.get("uses", "")).startswith("actions/setup-go@")]
    # A two-part version takes the newest patch release of that line, and
    # check-latest looks for it instead of settling for a cached older one.
    assert setup["with"] == {
        "go-version": "${{ steps.go-line.outputs.line }}",
        "check-latest": True,
    }
    assert steps.index(reader) < steps.index(setup) < steps.index(audit_steps()["audit-go"])


def test_the_go_line_step_fails_when_the_dockerfile_names_no_go_line(tmp_path: Path) -> None:
    # With an empty answer setup-go would install some other Go. The step
    # fails instead, and the Go audit then does not run at all.
    (tmp_path / "Dockerfile.orchestrator").write_text("FROM alpine:3.20\n", encoding="utf-8")
    done = _read_go_line(tmp_path)
    assert done.returncode != 0
    assert (tmp_path / "output").read_text(encoding="utf-8") == ""


# ─── The Python audit's loop ────────────────────────────────


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
        ["bash", "-e", "-c", audit_steps()["audit-python"]["run"]],
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
    command = tool_command(audit_steps()["audit-python"], "pip-audit")
    # --no-deps audits the pins as written; --disable-pip builds no environment.
    assert "--no-deps" in command and "--disable-pip" in command
    ci_python = [
        s
        for s in ci_job_steps("python")
        if str(s.get("uses", "")).startswith("actions/setup-python@")
    ]
    assert f"--python {ci_python[0]['with']['python-version']} " in command
