"""Pin ``scripts/checks/plan_status.py`` — stale progress rows.

The most common status-hygiene defect in the history: a PR merges and the
plan row that says "🔀 PR open (#N)" is never flipped. The checker reads
every progress table in the open cycle's plans and the RFC / issue PR plans,
and flags a row whose status cell says *open* or *not started* while every
PR it links to is already squash-merged on ``main``.
"""

from __future__ import annotations

from pathlib import Path

from scripts.checks.plan_status import find_stale_rows, target_docs

MERGED = frozenset({854, 855, 858})

DOC = """# vX plan

| PR | Title | Status | GitHub PR |
|----|-------|--------|-----------|
| 0 | plan | ✅ Merged | [#854](https://github.com/x/pull/854) |
| 1 | arc | 🔀 PR open — arc ran | [#855](https://github.com/x/pull/855) |
| 2 | docs | ⬜ Not started | — |
| 3 | bump | ⬜ Not started | [#858](https://github.com/x/pull/858) |
| 4 | mixed | 🔄 In progress — [#854](https://x/854) merged, [#999](https://x/999) open | — |
| 5 | open | 🔀 PR open | [#999](https://github.com/x/pull/999) |
"""


def test_a_pr_open_row_whose_pr_is_merged_is_stale() -> None:
    stale = find_stale_rows(DOC, MERGED)
    assert any(s.line == 6 and 855 in s.merged for s in stale)


def test_a_not_started_row_linking_a_merged_pr_is_stale() -> None:
    stale = find_stale_rows(DOC, MERGED)
    assert any(s.line == 8 and 858 in s.merged for s in stale)


def test_merged_rows_and_rows_without_links_are_not_stale() -> None:
    lines = {s.line for s in find_stale_rows(DOC, MERGED)}
    assert 5 not in lines  # ✅ Merged
    assert 7 not in lines  # ⬜ with no PR link


def test_in_progress_rows_are_left_alone_even_when_one_link_is_merged() -> None:
    """A 🔄 cell legitimately mixes merged and open PRs; only 🔀 / ⬜ are judged."""
    lines = {s.line for s in find_stale_rows(DOC, MERGED)}
    assert 9 not in lines


def test_a_pr_open_row_whose_pr_is_still_open_is_not_stale() -> None:
    lines = {s.line for s in find_stale_rows(DOC, MERGED)}
    assert 10 not in lines


def test_target_docs_skips_released_versions_and_keeps_pr_plans(tmp_path: Path) -> None:
    for rel in (
        "docs/v0.3.14-plan.md",              # released → frozen, skipped
        "docs/v0.3.15-plan.md",              # open cycle → checked
        "docs/v0.3.15-release-prep-plan.md", # open cycle → checked
        "docs/rfcs/0049-pr-plan.md",         # always checked
        "docs/issues/ISSUE-0082-residuals-pr-plan.md",  # always checked
        "docs/rfcs/0049-something.md",       # not a PR plan
        "docs/v0.3.14-release-checklist.md", # not a plan
    ):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# x\n", encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text("## [0.3.14] - 2026-08-19\n", encoding="utf-8")

    found = {p.relative_to(tmp_path).as_posix() for p in target_docs(tmp_path)}

    assert found == {
        "docs/v0.3.15-plan.md",
        "docs/v0.3.15-release-prep-plan.md",
        "docs/rfcs/0049-pr-plan.md",
        "docs/issues/ISSUE-0082-residuals-pr-plan.md",
    }
