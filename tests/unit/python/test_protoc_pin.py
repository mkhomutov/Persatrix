"""Pin the protoc release CI generates and checks the protobuf stubs with.

The stubs are committed, and CI's proto staleness check regenerates them and
fails on any difference, so CI must run the one protoc release they were made
with. Every job used to install it with arduino/setup-protoc, which downloads
from the release's github.com link. On 2026-11-08 that link answered every
request for the 34.1 files with HTTP 504 while GitHub's API still served
them, so each job that installs protoc failed before it tested anything.

One shared step, `.github/actions/setup-protoc`, now installs it: it downloads
through GitHub's API (`gh release download`), tries three times, and checks
the file against a pinned SHA-256 before unpacking it. These tests pin the
parts that make that true: the step names an exact release and its checksum,
every job that installs protoc uses the step, no workflow installs protoc
another way or names its version, a file that does not match is refused, and
the docs state the release the step installs.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from _test_infra import REPO_ROOT

GITHUB = REPO_ROOT / ".github"
WORKFLOWS = GITHUB / "workflows"
ACTION_DIR = "./.github/actions/setup-protoc"
ACTION_FILE = REPO_ROOT / ACTION_DIR / "action.yml"

# The jobs that install protoc today; a new one joins this list.
PROTOC_JOBS = {
    "ci.yml:proto-staleness",
    "ci.yml:python",
    "ci.yml:cost-regression-gate",
    "python-constraints-refresh.yml:refresh",
}
# Other ways a workflow could install protoc, each bypassing the pin.
OTHER_INSTALLS = ("setup-protoc@", "protobuf-compiler", "brew install protobuf")


def _step() -> dict[str, Any]:
    """The action's one step, which does the whole install."""
    action = yaml.safe_load(ACTION_FILE.read_text(encoding="utf-8"))
    assert action["runs"]["using"] == "composite"
    steps = action["runs"]["steps"]
    assert len(steps) == 1, "the install is one step, so its script can be run on its own"
    return dict(steps[0])


def _pins() -> tuple[str, str]:
    env = _step()["env"]
    return str(env["PROTOC_VERSION"]), str(env["PROTOC_SHA256"])


def _jobs() -> list[tuple[str, dict[str, Any]]]:
    """(`workflow:job`, job) for every job in every workflow."""
    jobs = []
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for job_id, job in (doc.get("jobs") or {}).items():
            jobs.append((f"{path.name}:{job_id}", job))
    return jobs


def test_the_step_pins_an_exact_release_and_its_checksum() -> None:
    version, sha256 = _pins()
    assert re.fullmatch(r"\d+\.\d+(\.\d+)?", version), f"{version!r} is not an exact release"
    assert re.fullmatch(r"[0-9a-f]{64}", sha256), "the checksum must be a full SHA-256"
    assert "${{" not in str(_step()["run"]), (
        "the script must take its values from env, so the test below can run it as it is"
    )


def test_every_job_that_installs_protoc_uses_the_shared_step() -> None:
    users = []
    for name, job in _jobs():
        steps = list(job.get("steps") or [])
        at = [i for i, step in enumerate(steps) if step.get("uses") == ACTION_DIR]
        if not at:
            continue
        users.append(name)
        assert len(at) == 1, f"{name} installs protoc more than once"
        checkout = [
            i for i, step in enumerate(steps)
            if str(step.get("uses", "")).startswith("actions/checkout@")
        ]
        assert checkout and checkout[0] < at[0], (
            f"{name} uses the local step before checking out the repository that holds it"
        )
    assert set(users) == PROTOC_JOBS, f"expected {sorted(PROTOC_JOBS)}, found {sorted(users)}"


def test_no_workflow_installs_protoc_another_way_or_names_its_version() -> None:
    """The version and checksum live in the step alone, so a bump is one
    change, and nothing installs a protoc the checksum does not cover."""
    version, _ = _pins()
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        text = path.read_text(encoding="utf-8")
        for other in OTHER_INSTALLS:
            assert other not in text, f"{path.name} installs protoc with {other!r}"
        assert not re.search(rf"(?<![\d.]){re.escape(version)}(?![\d.])", text), (
            f"{path.name} names protoc's version {version}; only the shared step may"
        )


def test_the_docs_state_the_release_the_step_installs() -> None:
    version, _ = _pins()
    for doc in ("CONTRIBUTING.md", "CLAUDE.md", "docs/methodology/automation-catalogue.md"):
        text = " ".join((REPO_ROOT / doc).read_text(encoding="utf-8").split())
        assert re.search(rf"protoc\*?\*? {re.escape(version)}\b", text), (
            f"{doc} does not state protoc {version}, the release CI installs"
        )


# ─── The script itself, run with stand-ins for gh and sleep ──────────────

needs_shell = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("sha256sum") is None,
    reason="the step's script needs bash and sha256sum",
)

FAKE_GH = """#!/bin/sh
echo "$*" >> "$FAKE_LOG"
[ -n "${FAKE_GH_FAILS:-}" ] && exit 1
while [ $# -gt 0 ]; do
  case "$1" in
    --dir) dir=$2; shift ;;
    --pattern) pattern=$2; shift ;;
  esac
  shift
done
printf 'not the protoc release' > "$dir/$pattern"
"""
FAKE_SLEEP = """#!/bin/sh
echo "sleep $*" >> "$FAKE_LOG"
"""


def _run(tmp_path: Path, **env: str) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run the step's script as a composite step runs it, on a fake runner."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("gh", FAKE_GH), ("sleep", FAKE_SLEEP)):
        tool = bin_dir / name
        tool.write_text(body, encoding="utf-8")
        tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    script = tmp_path / "step.sh"
    script.write_text(str(_step()["run"]), encoding="utf-8")
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    log = tmp_path / "calls.log"
    run_env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "RUNNER_OS": "Linux",
        "RUNNER_ARCH": "X64",
        "RUNNER_TEMP": str(runner_temp),
        "GITHUB_PATH": str(tmp_path / "github-path"),
        "FAKE_LOG": str(log),
        **{k: str(v) for k, v in _step()["env"].items() if k != "GH_TOKEN"},
        **env,
    }
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)],
        env=run_env, capture_output=True, text=True, timeout=60, check=False,
    )
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return result, calls


def _installed(tmp_path: Path) -> str:
    github_path = tmp_path / "github-path"
    return github_path.read_text(encoding="utf-8") if github_path.exists() else ""


@needs_shell
def test_a_file_that_does_not_match_the_checksum_is_refused(tmp_path: Path) -> None:
    version, _ = _pins()
    result, calls = _run(tmp_path)
    assert result.returncode != 0
    assert "does not match its pinned SHA-256" in result.stdout + result.stderr
    assert _installed(tmp_path) == "", "a refused file must not reach the PATH"
    assert not list((tmp_path / "runner-temp").glob("protoc-*/")), "nor be unpacked"
    assert calls == [
        f"release download v{version} --repo protocolbuffers/protobuf"
        f" --pattern protoc-{version}-linux-x86_64.zip --dir {tmp_path / 'runner-temp'}"
        " --clobber"
    ]


@needs_shell
def test_a_failed_download_is_tried_three_times(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, FAKE_GH_FAILS="1")
    assert result.returncode != 0
    assert "after three tries" in result.stdout + result.stderr
    downloads = [c for c in calls if c.startswith("release download")]
    pauses = [c for c in calls if c.startswith("sleep")]
    assert len(downloads) == 3 and pauses == ["sleep 10", "sleep 20"]
    assert _installed(tmp_path) == ""


@needs_shell
@pytest.mark.parametrize(("runner_os", "arch"), [("macOS", "ARM64"), ("Linux", "ARM64")])
def test_a_runner_with_no_pinned_file_is_refused(
    tmp_path: Path, runner_os: str, arch: str,
) -> None:
    """The checksum covers the Linux x86-64 file only."""
    result, calls = _run(tmp_path, RUNNER_OS=runner_os, RUNNER_ARCH=arch)
    assert result.returncode != 0
    assert "Linux x86-64" in result.stdout + result.stderr
    assert calls == [], "nothing is downloaded for a runner the pin does not cover"
