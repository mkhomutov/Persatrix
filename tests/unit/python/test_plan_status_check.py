"""Pin ``scripts/checks/plan_status.py`` — stale progress rows.

The most common status-hygiene defect in the history: a PR merges and the
plan row that says "🔀 PR open (#N)" is never flipped. The checker reads
every progress table in the open cycle's plans and the RFC / issue PR plans,
and flags a row whose status cell says *open* or *not started* while every
PR it links to is already squash-merged on ``main``. A PR writes its own row
before its number exists, so a 🔀 row that links nothing is judged by the
squash-merge that wrote the line instead.

The same check holds a patch release to one document: a scope-locks,
plan-amendment, release-prep-plan, release-baseline or release-checklist file
beside an unreleased patch release's plan fails (sequencing Amendment
2026-09-12, ruling (e)).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from scripts.checks.plan_status import (
    check_plan_status,
    find_stale_rows,
    pr_that_wrote,
    second_release_documents,
    target_docs,
)

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
| 6 | cites | ⬜ Not started — after [#855](https://github.com/x/pull/855) lands | — |
| 7 | own | 🔀 PR open | — (this PR) |
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


def test_a_not_started_row_that_merely_cites_a_merged_pr_in_prose_is_not_stale() -> None:
    """Only a cell that is nothing but a PR link counts for a ⬜ row."""
    lines = {s.line for s in find_stale_rows(DOC, MERGED)}
    assert 11 not in lines


def test_target_docs_skips_released_test_findings_plans(tmp_path: Path) -> None:
    for rel in ("docs/v0.3.7-test-findings-pr-plan.md", "docs/v0.3.15-test-findings-pr-plan.md"):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# x\n", encoding="utf-8")
    (tmp_path / "docs" / "rfcs").mkdir()
    (tmp_path / "docs" / "issues").mkdir()
    (tmp_path / "CHANGELOG.md").write_text("## [0.3.7] - 2026-06-06\n", encoding="utf-8")

    found = {p.relative_to(tmp_path).as_posix() for p in target_docs(tmp_path)}

    assert found == {"docs/v0.3.15-test-findings-pr-plan.md"}


def _touch(root: Path, *rels: str) -> None:
    for rel in rels:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# x\n", encoding="utf-8")


def test_an_open_patch_release_keeps_one_document(tmp_path: Path) -> None:
    """Scope locks, amendments, the prep plan, its baseline and the checklist belong in the plan."""
    _touch(
        tmp_path,
        "docs/v0.3.17-plan.md",
        "docs/v0.3.17-scope-locks.md",
        "docs/v0.3.17-plan-amendment-2026-10-01.md",
        "docs/v0.3.17-release-prep-plan.md",
        "docs/v0.3.17-release-baseline.md",
        "docs/v0.3.17-release-checklist.md",
    )
    (tmp_path / "CHANGELOG.md").write_text("## [0.3.16] - 2026-09-16\n", encoding="utf-8")

    assert second_release_documents(tmp_path) == [
        "docs/v0.3.17-plan-amendment-2026-10-01.md",
        "docs/v0.3.17-release-baseline.md",
        "docs/v0.3.17-release-checklist.md",
        "docs/v0.3.17-release-prep-plan.md",
        "docs/v0.3.17-scope-locks.md",
    ]


def test_released_files_the_report_and_a_findings_plan_are_not_second_documents(
    tmp_path: Path,
) -> None:
    """Releases before the ruling keep their files; the report and PR plans are not release docs."""
    _touch(
        tmp_path,
        "docs/v0.3.16-scope-locks.md",
        "docs/v0.3.16-release-checklist.md",
        "docs/v0.2-release-prep-plan.md",
        "docs/v0.3.17-plan.md",
        "docs/v0.3.17-test-findings-pr-plan.md",
        "docs/manual-tests/v0.3.17-execution-report.md",
    )
    (tmp_path / "CHANGELOG.md").write_text(
        "## [0.3.16] - 2026-09-16\n## [0.2.0] - 2026-04-18\n", encoding="utf-8",
    )
    assert second_release_documents(tmp_path) == []


def test_a_minor_release_is_not_judged(tmp_path: Path) -> None:
    """The ruling names patch releases; the amendment opening a minor release decides its shape."""
    _touch(tmp_path, "docs/v0.4.0-plan.md", "docs/v0.4.0-scope-locks.md")
    assert second_release_documents(tmp_path) == []


def test_a_second_document_fails_the_check_even_without_git_history(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The document rule reads the tree, so it still holds where no squash-merge history exists."""
    _touch(tmp_path, "docs/v0.3.17-plan.md", "docs/v0.3.17-release-checklist.md")

    assert check_plan_status(tmp_path) == 1
    out = capsys.readouterr().out
    assert "docs/v0.3.17-release-checklist.md" in out and "docs/v0.3.17-plan.md" in out


def test_an_unlinked_pr_open_row_is_stale_once_the_pr_that_wrote_it_has_merged() -> None:
    """Nothing links the row, so the squash-merge that wrote its line names the PR."""
    stale = find_stale_rows(DOC, MERGED, written_by={12: 858}.get)
    assert [(s.line, s.merged, s.unlinked) for s in stale if s.line == 12] == [(12, (858,), True)]


def test_an_unlinked_pr_open_row_is_not_stale_while_its_pr_is_open() -> None:
    """While the PR is open its row comes from a branch commit or the working tree: no writer."""
    lines = {s.line for s in find_stale_rows(DOC, MERGED, written_by=lambda _line: None)}
    assert 12 not in lines


def test_the_writer_only_stands_in_for_a_missing_link_on_a_pr_open_row() -> None:
    """A link outranks the writer (#999 is still open); ⬜ and 🔄 rows are never judged by it."""
    lines = {s.line for s in find_stale_rows(DOC, MERGED, written_by=lambda _line: 858)}
    assert lines == {6, 8, 12}


def test_a_row_without_its_closing_pipe_is_judged_as_github_renders_it() -> None:
    doc = "| PR | Status |\n|----|--------|\n| 1 | 🔀 PR open [#855](https://github.com/x/pull/855)\n"
    assert [s.line for s in find_stale_rows(doc, MERGED)] == [3]


def test_a_row_inside_a_code_fence_or_an_html_comment_is_not_judged() -> None:
    """A fenced or commented-out example is not a row; the lines it hides still count."""
    row = "| 1 | 🔀 PR open | [#855](https://github.com/x/pull/855) |"
    doc = "\n".join(["```md", row, "```", "<!--", row, "-->", row])
    assert [s.line for s in find_stale_rows(doc, MERGED)] == [7]


#: Fixture commits carry a fixed identity and nothing from the caller's git
#: setup — no hooks, signing or templates, no GIT_DIR pointing elsewhere.
_GIT_ENV = {
    **{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}

REL = "docs/rfcs/0099-pr-plan.md"
PLAN = (
    "| # | Title | Branch | Status | GitHub PR |\n"
    "|---|-------|--------|--------|-----------|\n"
    "| 1 | leaf | `feature/x-leaf` | ✅ Merged | [#1](https://github.com/x/pull/1) |\n"
)
OWN_ROW = "| 2 | closeout | `feature/x-close` | 🔀 PR open | — (this PR) |\n"  # line 4


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, env=_GIT_ENV)


def _plan_repo(repo: Path, plan: str, subject: str) -> Path:
    """A repository whose second commit, *subject*, writes *plan*.

    The first commit is empty: blame treats a root commit as a boundary, and
    a boundary commit is credited with nothing.
    """
    path = repo / REL
    path.parent.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "chore: root")
    path.write_bytes(plan.encode("utf-8"))  # bytes: no newline translation
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", subject)
    return path


def test_a_prs_own_unlinked_row_passes_while_open_and_fails_once_squash_merged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The row written uncommitted (pre-commit), then on a branch (PR open), then squash-merged."""
    plan = _plan_repo(tmp_path, PLAN, "docs(rfc0099): the plan (#1)")
    plan.write_text(PLAN + OWN_ROW, encoding="utf-8")
    assert check_plan_status(tmp_path) == 0
    _git(tmp_path, "commit", "-q", "-am", "docs(rfc0099): closeout")
    assert check_plan_status(tmp_path) == 0
    _git(tmp_path, "commit", "-q", "--amend", "-m", "docs(rfc0099): closeout (#2)")
    capsys.readouterr()

    assert check_plan_status(tmp_path) == 1
    out = capsys.readouterr().out
    assert f"{REL}:4" in out and "#2" in out


def test_a_plan_that_leaves_an_html_comment_open_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every row after an unclosed ``<!--`` or ``` is hidden, so none of them would be judged."""
    _plan_repo(tmp_path, "<!-- draft\n" + PLAN + OWN_ROW, "docs(rfc0099): closeout (#2)")
    capsys.readouterr()

    assert check_plan_status(tmp_path) == 1
    assert f"{REL}:1:" in capsys.readouterr().out


def test_a_second_document_fails_the_check_where_every_plan_row_is_current(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """With merge history and no stale row, a second release document still fails on its own."""
    _plan_repo(tmp_path, PLAN, "docs(rfc0099): the plan (#1)")
    _touch(tmp_path, "docs/v0.3.17-plan.md", "docs/v0.3.17-scope-locks.md")
    capsys.readouterr()

    assert check_plan_status(tmp_path) == 1
    assert "docs/v0.3.17-scope-locks.md: fold it into docs/v0.3.17-plan.md" in (
        capsys.readouterr().out
    )


def test_the_blamed_line_is_the_row_past_characters_python_would_split_on(tmp_path: Path) -> None:
    """git ends a line only at "\\n"; U+2028 and a lone "\\r" are ordinary text to it."""
    odd = PLAN.replace("| leaf |", "| leaf\u2028and\rmore |")
    _plan_repo(tmp_path, odd + OWN_ROW, "docs(rfc0099): closeout (#2)")
    assert check_plan_status(tmp_path) == 1


def test_a_shallow_clones_oldest_commit_is_not_credited_with_older_lines(tmp_path: Path) -> None:
    """Blame credits a shallow clone's boundary commit with every line older than it."""
    origin = tmp_path / "origin"
    plan = _plan_repo(origin, PLAN + OWN_ROW, "docs(rfc0099): closeout (#2)")
    plan.write_text(PLAN + OWN_ROW + "\n", encoding="utf-8")
    _git(origin, "commit", "-q", "-am", "docs(rfc0099): tidy (#3)")
    _git(tmp_path, "clone", "-q", "--depth", "1", origin.as_uri(), "shallow")

    assert pr_that_wrote(origin, REL, 4) == 2
    assert pr_that_wrote(tmp_path / "shallow", REL, 4) is None
