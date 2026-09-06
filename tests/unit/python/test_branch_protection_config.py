"""Pin the versioned required-status-check set (docs/methodology/branch-protection.json).

The set is a file so a change to it is a reviewed diff; the script shows the
difference against the live setting and applies it on request. These tests
pin the file's shape against the CI workflow and the pure diff logic; the
``gh api`` calls are not exercised.
"""

from __future__ import annotations

import re
from pathlib import Path

from scripts.release.branch_protection import diff, load_desired

REPO_ROOT = Path(__file__).resolve().parents[3]


def _ci_job_names() -> set[str]:
    text = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    return set(re.findall(r"^\s{4}name: (.+)$", text, re.M))


def test_every_required_context_is_a_ci_job_or_the_title_check() -> None:
    desired = load_desired()
    known = _ci_job_names() | {"Validate PR Title"}
    unknown = sorted(set(desired["contexts"]) - known)
    assert not unknown, f"contexts that no workflow produces: {unknown}"


def test_every_ci_job_is_required() -> None:
    """A job that runs but is not required can merge red; the file names all of them."""
    desired = load_desired()
    missing = sorted(_ci_job_names() - set(desired["contexts"]))
    assert not missing, f"CI jobs not in the required set: {missing}"


def test_diff_reports_both_directions_and_strict() -> None:
    desired = {"strict": True, "contexts": ["A", "B"]}
    assert diff({"strict": True, "contexts": ["A", "B"]}, desired) == []
    lines = diff({"strict": False, "contexts": ["A", "C"]}, desired)
    assert "strict: live=False file=True" in lines
    assert "+ required by file, not live: B" in lines
    assert "- live, not in file: C" in lines
