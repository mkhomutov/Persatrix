"""Pin the archival rule for version-cycle documents (ISSUE-0139).

A master plan, release-prep plan, scope-locks record, plan amendment, or
release baseline is *edited* while its version is in flight — the word cap
does useful work then — and *frozen* once the version's tag exists. Frozen
release documents are release evidence, the same category ``file_size.py``
already excludes by pattern for execution reports and checklists, so the
checker treats a version-cycle doc as excluded **once its version has a dated
CHANGELOG section** (written one PR before the tag) and as an ordinary capped
doc otherwise. The changelog, not ``git tag``, is the source so the answer is
the same in a depth-1 CI checkout, a worktree, and a tarball.

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
    _released_versions,
    _scan_files,
    _stale_allowlist_entries,
)

REPO_ROOT = Path(__file__).resolve().parents[3]

RELEASED = frozenset({"0.2.0", "0.3.0", "0.3.14"})


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
def test_every_version_cycle_kind_is_released_once_its_version_shipped(rel: str) -> None:
    assert _is_released_version_doc(rel, RELEASED)


def test_a_two_part_version_matches_its_patch_zero_release() -> None:
    """``docs/v0.2-release-prep-plan.md`` was released as ``0.2.0``."""
    assert _is_released_version_doc("docs/v0.2-release-prep-plan.md", RELEASED)


def test_an_unreleased_version_is_not_released() -> None:
    assert not _is_released_version_doc("docs/v0.3.15-plan.md", RELEASED)
    assert not _is_released_version_doc("docs/v9.9.9-release-prep-plan.md", RELEASED)


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
    assert not _is_released_version_doc(rel, RELEASED)


def test_no_changelog_means_nothing_is_released() -> None:
    """A tree without a changelog falls back to capping everything."""
    assert not _is_released_version_doc("docs/v0.3.14-plan.md", frozenset())


# --------------------------------------------------------------------------
# The scan
# --------------------------------------------------------------------------


def test_released_plans_are_excluded_from_the_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A released plan is neither measured nor flagged — it is evidence now."""
    monkeypatch.setattr(file_size, "_released_versions", lambda _root: RELEASED)
    _write(tmp_path, "docs/v0.3.14-plan.md", DEFAULT_MAX_DOC_WORDS + 50)
    _write(tmp_path, "docs/v0.3.14-release-prep-plan.md", DEFAULT_MAX_DOC_WORDS + 50)

    warnings, _, doc_results = _scan_files(tmp_path)

    assert "docs/v0.3.14-plan.md" not in dict(doc_results)
    assert "docs/v0.3.14-release-prep-plan.md" not in dict(doc_results)
    assert not warnings


def test_the_open_cycles_plan_is_still_capped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The plan being edited right now keeps its cap — only shipping frees it."""
    monkeypatch.setattr(file_size, "_released_versions", lambda _root: RELEASED)
    _write(tmp_path, "docs/v0.3.15-plan.md", DEFAULT_MAX_DOC_WORDS + 50)

    warnings, _, doc_results = _scan_files(tmp_path)

    assert "docs/v0.3.15-plan.md" in dict(doc_results)
    assert {w.file for w in warnings} == {"docs/v0.3.15-plan.md"}


def test_released_versions_reads_this_repos_changelog() -> None:
    released = _released_versions(REPO_ROOT)
    assert "0.3.0" in released
    assert all(v.count(".") == 2 for v in released)


def test_released_versions_is_empty_without_a_changelog(tmp_path: Path) -> None:
    assert _released_versions(tmp_path) == frozenset()


def test_released_versions_ignores_a_directory_nested_inside_the_repo() -> None:
    """A scan rooted below the top level must not inherit the root's releases.

    Otherwise a pytest ``tmp_path`` that happens to live under a checkout
    would silently treat a fixture named after a real version as released.
    """
    assert _released_versions(REPO_ROOT / "docs") == frozenset()


def test_the_unreleased_section_does_not_count(tmp_path: Path) -> None:
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n- stuff\n\n"
        "## [0.3.15] - 2026-09-09\n\n## [0.3.14] - 2026-08-19\n",
        encoding="utf-8",
    )
    assert _released_versions(tmp_path) == frozenset({"0.3.15", "0.3.14"})


# --------------------------------------------------------------------------
# Allowlist hygiene: released docs are reported, not failed
# --------------------------------------------------------------------------


def test_stale_allowlist_entries_names_released_docs_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The open cycle's plan is not stale; a tagged one is."""
    monkeypatch.setattr(
        file_size, "GRANDFATHERED_FILES",
        frozenset({"docs/v0.3.14-plan.md", "docs/v0.3.15-plan.md", "ROADMAP.md"}),
    )
    assert _stale_allowlist_entries(RELEASED) == ["docs/v0.3.14-plan.md"]


def test_stale_allowlist_is_a_notice_not_a_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """A release dated while the plan is still allowlisted must not turn CI red.

    The entry is retired by the post-release follow-up; until then every
    unrelated PR would otherwise fail on it. So: printed, exit code untouched.
    """
    monkeypatch.setattr(file_size, "_released_versions", lambda _root: RELEASED)
    monkeypatch.setattr(file_size, "GRANDFATHERED_FILES", frozenset({"docs/v0.3.14-plan.md"}))
    _write(tmp_path, "docs/v0.3.14-plan.md", DEFAULT_MAX_DOC_WORDS + 50)

    rc = file_size.check_file_size(tmp_path, strict=True)

    assert rc == 0
    assert "[STALE-ALLOWLIST] docs/v0.3.14-plan.md" in capsys.readouterr().out
