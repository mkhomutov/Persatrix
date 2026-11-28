"""Pin the versioned required-status-check set (docs/methodology/branch-protection.json).

The set is a file so a change to it is a reviewed diff; the script shows the
difference against the live setting and applies it on request. These tests
pin the file's shape against the CI workflow and the pure diff logic; the
``gh api`` calls are not exercised.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from scripts.release.branch_protection import CONFIG, diff, load_desired

REPO_ROOT = Path(__file__).resolve().parents[3]


def _ci_job_names() -> set[str]:
    text = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    return set(re.findall(r"^\s{4}name: (.+)$", text, re.M))


def _not_required() -> dict[str, str]:
    """The CI jobs the file names as not required, each with its reason."""
    named = json.loads(CONFIG.read_text(encoding="utf-8")).get("not_required") or {}
    return {str(job): str(reason) for job, reason in named.items()}


def test_every_required_context_is_a_ci_job_or_the_title_check() -> None:
    desired = load_desired()
    known = _ci_job_names() | {"Validate PR Title"}
    unknown = sorted(set(desired["contexts"]) - known)
    assert not unknown, f"contexts that no workflow produces: {unknown}"


def test_every_ci_job_is_required_or_named_as_not_required() -> None:
    """A job that runs but is not required can merge red. The file names every
    job: in `contexts`, or under `not_required` with the reason, so a job
    outside the required set is a reviewed choice and never an oversight."""
    desired = load_desired()
    missing = sorted(_ci_job_names() - set(desired["contexts"]) - set(_not_required()))
    assert not missing, f"CI jobs that are neither required nor named as not required: {missing}"


def test_a_job_named_as_not_required_is_a_ci_job_with_a_reason_and_is_not_required_too() -> None:
    named = _not_required()
    assert not sorted(set(named) - _ci_job_names()), "named as not required, and no CI job"
    assert not sorted(set(named) & set(load_desired()["contexts"])), "both required and not"
    for job, reason in named.items():
        assert len(reason.split()) >= 5, f"{job}: say why it is not required"


def test_no_job_is_named_as_not_required() -> None:
    """The list itself is pinned, and it is empty: every CI job is in the
    required set since `Python 3.12 (unit tests)` joined it (ruling (ab) of
    the sequencing Amendment 2026-11-28). A job left out of the set is a
    change to make here on purpose, not one that rides along."""
    assert sorted(_not_required()) == []


def test_the_required_set_is_every_ci_job_and_the_title_check() -> None:
    """`make branch-protection-apply` sends `contexts` and nothing else."""
    assert set(load_desired()) == {"strict", "contexts"}
    assert "Python 3.12 (unit tests)" in load_desired()["contexts"]
    assert set(load_desired()["contexts"]) == _ci_job_names() | {"Validate PR Title"}
    assert len(load_desired()["contexts"]) == 13


def test_diff_reports_both_directions_and_strict() -> None:
    desired = {"strict": True, "contexts": ["A", "B"]}
    assert diff({"strict": True, "contexts": ["A", "B"]}, desired) == []
    lines = diff({"strict": False, "contexts": ["A", "C"]}, desired)
    assert "strict: live=False file=True" in lines
    assert "+ required by file, not live: B" in lines
    assert "- live, not in file: C" in lines
