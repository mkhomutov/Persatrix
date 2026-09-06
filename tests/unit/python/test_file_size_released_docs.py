"""Pin the archival rule for version-cycle documents (ISSUE-0139).

A master plan, release-prep plan, scope-locks record, plan amendment, or
release baseline is *edited* while its version is in flight — the word cap
does useful work then — and *frozen* once the version's tag exists. Frozen
release documents are release evidence, the same category ``file_size.py``
already excludes by pattern for execution reports and checklists, so the
checker treats a version-cycle doc as excluded **when its tag exists** and
as an ordinary capped doc otherwise.

Without this, every released plan needed a hand-written allowlist entry
whose exit condition ("remove once archived") nothing could execute — nine
had accumulated by v0.3.14 (#838).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.checks import file_size
from scripts.checks.file_size import (
    DEFAULT_MAX_DOC_WORDS,
    _is_released_version_doc,
    _released_tags,
    _scan_files,
)
from scripts.checks.file_size_allowlist import GRANDFATHERED_FILES

REPO_ROOT = Path(__file__).resolve().parents[3]

TAGS = frozenset({"v0.2.0", "v0.3.0", "v0.3.14"})


def _write(root: Path, rel: str, words: int) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(" ".join(["word"] * words) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# The predicate
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rel",
    [
        "docs/v0.3.14-plan.md",
        "docs/v0.3.14-scope-locks.md",
        "docs/v0.3.14-release-prep-plan.md",
        "docs/v0.3.14-release-baseline.md",
        "docs/v0.3.14-plan-amendment-2026-08-10.md",
    ],
)
def test_every_version_cycle_kind_is_released_once_its_tag_exists(rel: str) -> None:
    assert _is_released_version_doc(rel, TAGS)


def test_a_two_part_version_matches_its_patch_zero_tag() -> None:
    """``docs/v0.2-release-prep-plan.md`` was released as ``v0.2.0``."""
    assert _is_released_version_doc("docs/v0.2-release-prep-plan.md", TAGS)


def test_an_unreleased_version_is_not_released() -> None:
    assert not _is_released_version_doc("docs/v0.3.15-plan.md", TAGS)
    assert not _is_released_version_doc("docs/v9.9.9-release-prep-plan.md", TAGS)


@pytest.mark.parametrize(
    "rel",
    [
        # Living documents that merely start with a version-ish prefix.
        "docs/v0.3.x-sequencing.md",
        # Already excluded by pattern; not this predicate's job.
        "docs/v0.3.14-release-checklist.md",
        "docs/manual-tests/v0.3.14-execution-report.md",
        # Unrelated docs.
        "docs/v0.3.14-something-else.md",
        "docs/rfcs/0014-agent-skill-registry-lifecycle.md",
        "ROADMAP.md",
    ],
)
def test_the_predicate_stays_narrow(rel: str) -> None:
    assert not _is_released_version_doc(rel, TAGS)


def test_no_tags_means_nothing_is_released() -> None:
    """A tarball or a checkout without tags falls back to capping everything."""
    assert not _is_released_version_doc("docs/v0.3.14-plan.md", frozenset())


# --------------------------------------------------------------------------
# The scan
# --------------------------------------------------------------------------


def test_released_plans_are_excluded_from_the_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A released plan is neither measured nor flagged — it is evidence now."""
    monkeypatch.setattr(file_size, "_released_tags", lambda _root: TAGS)
    _write(tmp_path, "docs/v0.3.14-plan.md", DEFAULT_MAX_DOC_WORDS + 50)
    _write(tmp_path, "docs/v0.3.14-release-prep-plan.md", DEFAULT_MAX_DOC_WORDS + 50)

    warnings, _, doc_results = _scan_files(tmp_path)

    assert "docs/v0.3.14-plan.md" not in dict(doc_results)
    assert "docs/v0.3.14-release-prep-plan.md" not in dict(doc_results)
    assert not warnings


def test_the_open_cycles_plan_is_still_capped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The plan being edited right now keeps its cap — only the tag frees it."""
    monkeypatch.setattr(file_size, "_released_tags", lambda _root: TAGS)
    _write(tmp_path, "docs/v0.3.15-plan.md", DEFAULT_MAX_DOC_WORDS + 50)

    warnings, _, doc_results = _scan_files(tmp_path)

    assert "docs/v0.3.15-plan.md" in dict(doc_results)
    assert {w.file for w in warnings} == {"docs/v0.3.15-plan.md"}


def test_released_tags_reads_this_repos_tags() -> None:
    tags = _released_tags(REPO_ROOT)
    assert "v0.3.0" in tags
    assert all(t.startswith("v") and t[1].isdigit() for t in tags)


def test_released_tags_is_empty_outside_a_repository(tmp_path: Path) -> None:
    assert _released_tags(tmp_path) == frozenset()


# --------------------------------------------------------------------------
# Allowlist hygiene: released docs must not also be allowlisted
# --------------------------------------------------------------------------


def test_allowlist_holds_no_released_version_docs() -> None:
    """Once the tag exists the entry is dead weight — and the reason this
    mechanism exists is so those entries stop accumulating."""
    tags = _released_tags(REPO_ROOT)
    stale = sorted(rel for rel in GRANDFATHERED_FILES if _is_released_version_doc(rel, tags))
    assert not stale, (
        "these allowlist entries are released version-cycle docs, now excluded by "
        f"the archival rule — drop them from file_size_allowlist.py: {stale}"
    )
