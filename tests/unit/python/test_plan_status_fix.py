"""Pin how a merged PR's plan row catches up: the lag the check allows, and the writer.

A PR cannot write ✅ and a merge date before it has merged, so its row still
says 🔀 when it lands (ISSUE-0190). ``docs/merged-prs.md`` has the same problem
and the same answer, so the row follows that file: it may trail while the
history has no row for its PR, and the commit that adds that row flips it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _git_fixture_helpers import git as _git

from scripts.checks import SKIPPED
from scripts.checks.plan_status import check_plan_status, fix_stale_rows
from scripts.merged_prs import MergedPR, render

from ._plan_status_test_helpers import (
    DONE,
    FLIPPED,
    HEADER,
    HISTORY,
    OPEN,
    OTHER,
    PLAN,
    RECORDED,
    REL,
    URL,
)
from ._plan_status_test_helpers import plan_repo as _repo
from ._plan_status_test_helpers import staged_blob as _staged_blob
from ._plan_status_test_helpers import (
    stop_rebase_on_a_conflict as _stop_rebase_on_a_conflict,
)

# --- The lag ---------------------------------------------------------------


def test_the_newest_merge_may_leave_its_own_row_open(tmp_path: Path) -> None:
    _repo(tmp_path, PLAN, "feat: next (#2)")
    assert check_plan_status(tmp_path) == 0


def test_the_newest_merge_may_leave_its_own_unlinked_row_open(tmp_path: Path) -> None:
    own = "| 2 | A1 | next | `feature/x-next` | 🔀 | — (this PR) | — |\n"
    _repo(tmp_path, HEADER + DONE + own, "feat: next (#2)")
    assert check_plan_status(tmp_path) == 0


def test_a_merge_that_carried_no_flip_does_not_fail_the_row_before_it(tmp_path: Path) -> None:
    """A Dependabot PR, or a branch only rebased, lands with no commit the hook ran on.

    About one merge in four does, so the row may not fail just because it is no
    longer the newest. The history trails by the same merges.
    """
    _repo(tmp_path, PLAN, "feat: next (#2)", "chore(deps): bump (#3)", "chore(deps): bump (#4)")
    assert check_plan_status(tmp_path) == 0


def test_a_row_fails_once_the_history_records_its_pr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A commit that added the history row had the chance to flip this one."""
    _repo(tmp_path, PLAN, "feat: next (#2)", recorded=(2, 1))
    capsys.readouterr()

    assert check_plan_status(tmp_path) == 1
    assert f"{REL}:4" in capsys.readouterr().out


def test_a_row_of_several_prs_trails_until_the_history_records_them_all(tmp_path: Path) -> None:
    row = f"| 2 | A1 | next | `feature/x-next` | 🔀 | [#1]({URL}/1), [#2]({URL}/2) | — |\n"
    _repo(tmp_path, HEADER + row, "feat: next (#2)")
    assert check_plan_status(tmp_path) == 0


def test_a_not_started_row_gets_no_lag(tmp_path: Path) -> None:
    """A merge leaves its row at 🔀; ⬜ beside a merged PR's link is a row nobody updated."""
    _repo(tmp_path, PLAN.replace("🔀", "⬜"), "feat: next (#2)")
    assert check_plan_status(tmp_path) == 1


def test_a_lost_history_tolerates_nothing(tmp_path: Path) -> None:
    """With no rows to read every merge would look newer than the file, and every row would pass."""
    path = _repo(tmp_path, PLAN, "feat: next (#2)")
    (tmp_path / HISTORY).unlink()
    assert check_plan_status(tmp_path) == 1

    (tmp_path / HISTORY).write_text("# reformatted, no rows\n", encoding="utf-8")
    assert path.exists() and check_plan_status(tmp_path) == 1


def test_a_trailing_row_is_named_so_the_lag_is_visible(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _repo(tmp_path, PLAN, "feat: next (#2)")
    capsys.readouterr()

    assert check_plan_status(tmp_path) == 0
    out = capsys.readouterr().out
    assert f"{REL}:4" in out and "#2" in out and "[FAIL]" not in out


# --- The writer ------------------------------------------------------------


def test_fix_flips_the_status_and_writes_the_date_the_history_gives(tmp_path: Path) -> None:
    path = _repo(tmp_path, PLAN, "feat: next (#2)", recorded=(2, 1))

    assert fix_stale_rows(tmp_path) == 0

    assert path.read_text(encoding="utf-8") == HEADER + DONE + FLIPPED
    assert check_plan_status(tmp_path) == 0


def test_fix_links_an_unlinked_own_row(tmp_path: Path) -> None:
    own = "| 2 | A1 | next | `feature/x-next` | 🔀 | — (this PR) | — |\n"
    path = _repo(tmp_path, HEADER + DONE + own, "feat: next (#2)", recorded=(2, 1))

    assert fix_stale_rows(tmp_path) == 0

    assert path.read_text(encoding="utf-8") == HEADER + DONE + FLIPPED


def test_fix_keeps_the_word_on_a_worded_status(tmp_path: Path) -> None:
    worded = PLAN.replace("| ✅ |", "| ✅ Merged |").replace("| 🔀 |", "| 🔀 PR open |")
    path = _repo(tmp_path, worded, "feat: next (#2)", recorded=(2, 1))

    assert fix_stale_rows(tmp_path) == 0

    assert f"| ✅ Merged | [#2]({URL}/2) | {RECORDED} |" in path.read_text(encoding="utf-8")


def test_fix_flips_a_not_started_row_that_links_its_merged_pr(tmp_path: Path) -> None:
    path = _repo(tmp_path, PLAN.replace("🔀", "⬜"), "feat: next (#2)", recorded=(2, 1))

    assert fix_stale_rows(tmp_path) == 0

    assert path.read_text(encoding="utf-8") == HEADER + DONE + FLIPPED


def test_fix_writes_the_date_into_the_status_of_a_table_with_no_merged_column(
    tmp_path: Path,
) -> None:
    plan = (
        "| PR | Title | Status | GitHub PR |\n"
        "|----|-------|--------|-----------|\n"
        f"| 2 | next | 🔀 PR open | [#2]({URL}/2) |\n"
    )
    path = _repo(tmp_path, plan, "feat: next (#2)", recorded=(2, 1))

    assert fix_stale_rows(tmp_path) == 0

    assert f"| 2 | next | ✅ Merged {RECORDED} | [#2]({URL}/2) |\n" in (
        path.read_text(encoding="utf-8")
    )


def test_fix_keeps_link_and_date_in_the_status_of_a_table_with_neither_column(
    tmp_path: Path,
) -> None:
    """The shape of a version plan's Progress table: `✅ [#1047](…)`."""
    plan = (
        "| # | RFC phase | Status |\n"
        "|---|-----------|--------|\n"
        f"| A1 | society tables | 🔀 [#2]({URL}/2) |\n"
    )
    path = _repo(tmp_path, plan, "feat: next (#2)", recorded=(2, 1))

    assert fix_stale_rows(tmp_path) == 0

    assert f"| A1 | society tables | ✅ [#2]({URL}/2) {RECORDED} |\n" in (
        path.read_text(encoding="utf-8")
    )


def test_fix_dates_a_row_of_several_prs_by_the_later_merge(tmp_path: Path) -> None:
    row = f"| 2 | A1 | next | `feature/x-next` | 🔀 | [#1]({URL}/1), [#2]({URL}/2) | — |\n"
    path = _repo(tmp_path, HEADER + row, "feat: next (#2)", recorded=(2, 1))
    history = tmp_path / HISTORY
    rows = [MergedPR(2, "2026-11-02", "change 2"), MergedPR(1, "2026-10-31", "change 1")]
    history.write_text(render(rows), encoding="utf-8")

    assert fix_stale_rows(tmp_path) == 0

    assert f"[#1]({URL}/1), [#2]({URL}/2) | 2026-11-02 |" in path.read_text(encoding="utf-8")


def test_fix_leaves_a_row_with_words_it_would_lose_and_the_check_still_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only a person can say what becomes of "arc ran"; the row stays for them."""
    plan = PLAN.replace("| 🔀 |", "| 🔀 PR open — arc ran |")
    path = _repo(tmp_path, plan, "feat: next (#2)", recorded=(2, 1))
    capsys.readouterr()

    assert fix_stale_rows(tmp_path) == 0

    assert path.read_text(encoding="utf-8") == plan
    assert f"{REL}:4" in capsys.readouterr().out
    assert check_plan_status(tmp_path) == 1


def test_fix_leaves_a_row_whose_link_is_not_in_the_pr_cell(tmp_path: Path) -> None:
    """A link in the title cites a merged PR; it does not say the row's own PR has merged."""
    plan = HEADER + f"| 2 | A1 | after [#1]({URL}/1) | `feature/x-next` | 🔀 | — | — |\n"
    path = _repo(tmp_path, plan, "feat: next (#2)", recorded=(2, 1))

    assert fix_stale_rows(tmp_path) == 0

    assert path.read_text(encoding="utf-8") == plan


def test_fix_leaves_a_row_whose_merged_cell_already_holds_something(tmp_path: Path) -> None:
    plan = PLAN.replace("| — |\n", "| see notes |\n")
    path = _repo(tmp_path, plan, "feat: next (#2)", recorded=(2, 1))

    assert fix_stale_rows(tmp_path) == 0

    assert path.read_text(encoding="utf-8") == plan


def test_fix_waits_for_the_history_to_give_the_date(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Run by hand before the history catches up, it has no date to write, and nothing is wrong."""
    path = _repo(tmp_path, PLAN, "feat: next (#2)")
    capsys.readouterr()

    assert fix_stale_rows(tmp_path) == 0

    assert path.read_text(encoding="utf-8") == PLAN
    assert capsys.readouterr().out == ""


def test_fix_leaves_a_row_inside_a_code_fence(tmp_path: Path) -> None:
    plan = f"```md\n{OPEN}```\n" + PLAN
    path = _repo(tmp_path, plan, "feat: next (#2)", recorded=(2, 1))

    assert fix_stale_rows(tmp_path) == 0

    assert path.read_text(encoding="utf-8") == f"```md\n{OPEN}```\n" + HEADER + DONE + FLIPPED


def test_fix_changes_nothing_on_a_second_run(tmp_path: Path) -> None:
    path = _repo(tmp_path, PLAN, "feat: next (#2)", recorded=(2, 1))
    assert fix_stale_rows(tmp_path) == 0
    once = path.read_bytes()

    assert fix_stale_rows(tmp_path) == 0

    assert path.read_bytes() == once


def test_fix_keeps_every_other_byte_of_the_plan(tmp_path: Path) -> None:
    """Line endings and characters Python would split on are git's, not the writer's to change."""
    plan = "# plan\r\n odd\n" + PLAN + "no newline at the end"
    path = _repo(tmp_path, plan, "feat: next (#2)", recorded=(2, 1))

    assert fix_stale_rows(tmp_path) == 0

    want = "# plan\r\n odd\n" + HEADER + DONE + FLIPPED + "no newline at the end"
    assert path.read_bytes() == want.encode("utf-8")


@pytest.mark.parametrize(
    ("status", "pr_cell", "flipped"),
    [
        pytest.param(
            "🔀 PR open (this PR)", "—", f"✅ Merged | [#2]({URL}/2)", id="own-row-worded",
        ),
        pytest.param("🔀", "_this PR_", f"✅ | [#2]({URL}/2)", id="own-row-italic-placeholder"),
    ],
)
def test_fix_flips_the_other_ways_a_plan_writes_its_own_row(
    tmp_path: Path, status: str, pr_cell: str, flipped: str
) -> None:
    row = f"| 2 | A1 | next | `feature/x-next` | {status} | {pr_cell} | — |\n"
    path = _repo(tmp_path, HEADER + row, "feat: next (#2)", recorded=(2, 1))

    assert fix_stale_rows(tmp_path) == 0

    assert f"| {flipped} | {RECORDED} |\n" in path.read_text(encoding="utf-8")


def test_fix_flips_the_v0316_form_of_a_status_with_its_link_inside(tmp_path: Path) -> None:
    plan = (
        "| # | RFC phase | Status |\n"
        "|---|-----------|--------|\n"
        f"| A1 | society tables | 🔀 [#2]({URL}/2) open |\n"
    )
    path = _repo(tmp_path, plan, "feat: next (#2)", recorded=(2, 1))

    assert fix_stale_rows(tmp_path) == 0

    assert f"| A1 | society tables | ✅ [#2]({URL}/2) {RECORDED} |\n" in (
        path.read_text(encoding="utf-8")
    )


@pytest.mark.parametrize(
    "plan",
    [
        pytest.param(HEADER + f"| 2 | next | 🔀 | [#2]({URL}/2) | — |\n", id="a-cell-short"),
        pytest.param(
            "| PR | Note | GitHub PR | Merged |\n|---|---|---|---|\n"
            f"| 2 | 🔀 | [#2]({URL}/2) | — |\n",
            id="marker-outside-the-status-column",
        ),
        pytest.param(
            HEADER + "| 2 | A1 | next | `feature/x-next` | 🔀 | see the notes | — |\n",
            id="unlinked-row-with-words-in-its-pr-cell",
        ),
    ],
)
def test_fix_leaves_a_row_it_cannot_read_for_certain(tmp_path: Path, plan: str) -> None:
    path = _repo(tmp_path, plan, "feat: next (#2)", recorded=(2, 1))

    assert fix_stale_rows(tmp_path) == 0

    assert path.read_text(encoding="utf-8") == plan


def test_fix_keeps_a_byte_that_is_not_utf8_in_both_copies(tmp_path: Path) -> None:
    """A plan is never rewritten through a decode that would turn a bad byte into U+FFFD."""
    path = _repo(tmp_path, PLAN, "feat: next (#2)", recorded=(2, 1))
    data = b"\xff not utf-8\n" + PLAN.encode("utf-8")
    path.write_bytes(data)
    _git(tmp_path, "add", REL)

    assert fix_stale_rows(tmp_path) == 0

    assert path.read_bytes().startswith(b"\xff not utf-8\n")
    assert _staged_blob(tmp_path).startswith(b"\xff not utf-8\n")
    assert FLIPPED.encode("utf-8") in _staged_blob(tmp_path)


def test_a_shallow_clone_flips_what_its_history_records(tmp_path: Path) -> None:
    """A PR with a history row has merged, however short the log is.

    The hook has just added that row, so skipping here would commit a history
    that says the PR merged beside a row that says it is open, and CI fails that.
    """
    origin = tmp_path / "origin"
    _repo(origin, PLAN, "feat: next (#2)", "fix: other (#3)", recorded=(2, 1))
    _git(tmp_path, "clone", "-q", "--depth", "1", origin.as_uri(), "shallow")

    assert fix_stale_rows(tmp_path / "shallow") == 0

    assert (tmp_path / "shallow" / REL).read_text(encoding="utf-8") == HEADER + DONE + FLIPPED


def _branch_off_main(repo: Path) -> None:
    """Put *repo* on a branch, with ``origin/main`` where a fetched ``main`` would be."""
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "switch", "-q", "-c", "topic")


def test_a_branch_commit_quoting_an_open_pr_does_not_flip_its_row(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A subject ending ``(#3)`` proves nothing on a branch: #3 may be this very PR, still open.

    The history generator is fooled by that subject, so the row must not be
    flipped on its word: nothing would ever check a ✅ on an open PR.
    """
    later = f"| 3 | A1 | later | `feature/x-later` | 🔀 | [#3]({URL}/3) | — |\n"
    path = _repo(tmp_path, PLAN + later, "feat: next (#2)", recorded=(2, 1))
    _branch_off_main(tmp_path)
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "fix: review findings (#3)")
    rows = [MergedPR(n, RECORDED, f"change {n}") for n in (3, 2, 1)]
    (tmp_path / HISTORY).write_text(render(rows), encoding="utf-8")
    capsys.readouterr()

    assert fix_stale_rows(tmp_path) == 0

    assert later in path.read_text(encoding="utf-8")
    assert "#3" in capsys.readouterr().out
    assert check_plan_status(tmp_path) == 1


def test_a_branch_commit_quoting_a_merged_pr_does_not_date_its_row(tmp_path: Path) -> None:
    """The history takes the newest commit naming a PR, so its date would be the branch's."""
    path = _repo(tmp_path, PLAN, "feat: next (#2)", recorded=(2, 1))
    _branch_off_main(tmp_path)
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "fix: review follow-up (#2)")

    assert fix_stale_rows(tmp_path) == 0

    assert path.read_text(encoding="utf-8") == PLAN


def test_a_real_merge_still_flips_on_a_branch_that_quotes_nothing(tmp_path: Path) -> None:
    path = _repo(tmp_path, PLAN, "feat: next (#2)", recorded=(2, 1))
    _branch_off_main(tmp_path)
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "fix: an ordinary commit")

    assert fix_stale_rows(tmp_path) == 0

    assert path.read_text(encoding="utf-8") == HEADER + DONE + FLIPPED


def test_a_conflict_left_unresolved_stops_the_writer_touching_any_plan(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Row numbers and the log are mid-flight, and a conflicted file holds both sides."""
    plan = _repo(tmp_path, PLAN, "feat: next (#2)", recorded=(2, 1))
    conflicted = _stop_rebase_on_a_conflict(tmp_path, plan)
    capsys.readouterr()

    assert fix_stale_rows(tmp_path) == SKIPPED

    assert plan.read_bytes() == conflicted
    assert (tmp_path / OTHER).read_text(encoding="utf-8") == PLAN
    assert "conflict" in capsys.readouterr().out
