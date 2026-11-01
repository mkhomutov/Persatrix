"""Pin how a merged PR's plan row catches up: the lag the check allows, and the writer.

A PR cannot write ✅ and a merge date before it has merged, so its row still
says 🔀 when it lands (ISSUE-0190). ``docs/merged-prs.md`` has the same problem
and the same answer, so the row follows that file: it may trail while the
history has no row for its PR, and the commit that adds that row flips it.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from _git_fixture_helpers import GIT_ENV
from _git_fixture_helpers import git as _git
from _git_fixture_helpers import run as _run

from scripts.checks import SKIPPED
from scripts.checks.plan_status import check_plan_status, fix_stale_rows
from scripts.merged_prs import REPO_ROOT, MergedPR, render

HISTORY = "docs/merged-prs.md"
REL = "docs/rfcs/0099-pr-plan.md"
URL = "https://github.com/mkhomutov/Persatrix/pull"
#: Not the day the fixture commits, so a date on a row can only come from the history.
RECORDED = "2026-10-31"
HEADER = (
    "| PR | Row | Title | Branch | Status | GitHub PR | Merged |\n"
    "|----|-----|-------|--------|--------|-----------|--------|\n"
)
DONE = f"| 1 | A1 | leaf | `feature/x-leaf` | ✅ | [#1]({URL}/1) | 2026-10-30 |\n"
OPEN = f"| 2 | A1 | next | `feature/x-next` | 🔀 | [#2]({URL}/2) | — |\n"
FLIPPED = f"| 2 | A1 | next | `feature/x-next` | ✅ | [#2]({URL}/2) | {RECORDED} |\n"
PLAN = HEADER + DONE + OPEN


def _repo(repo: Path, plan: str, *merges: str, recorded: tuple[int, ...] = (1,)) -> Path:
    """A repository where *merges* land in turn, the first writing *plan*, and returns the plan.

    The history records the PRs in *recorded*, as the last commit that ran the
    hook left it.
    """
    path = repo / REL
    path.parent.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "chore: root")
    rows = [MergedPR(n, RECORDED, f"change {n}") for n in recorded]
    (repo / HISTORY).write_text(render(rows), encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "docs: the history (#1)")
    path.write_bytes(plan.encode("utf-8"))  # bytes: no newline translation
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", merges[0])
    for subject in merges[1:]:
        _git(repo, "commit", "-q", "--allow-empty", "-m", subject)
    return path


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


def _staged_blob(repo: Path) -> bytes:
    """The plan as the next commit would hold it, byte for byte."""
    proc = subprocess.run(
        ["git", "show", f":{REL}"], cwd=repo, capture_output=True, check=True, env=GIT_ENV
    )
    return proc.stdout


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


OTHER = "docs/rfcs/0098-pr-plan.md"


def _stop_rebase_on_a_conflict(repo: Path, plan: Path) -> bytes:
    """Stop a rebase on a conflict in *plan*, with *OTHER* a clean plan holding a stale row.

    Returns the conflicted file's bytes.
    """
    (repo / OTHER).write_bytes(PLAN.encode("utf-8"))
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "docs: another plan")
    _git(repo, "switch", "-q", "-c", "topic")
    plan.write_bytes((PLAN + "| 3 | A1 | ours | x | ⬜ | — | — |\n").encode("utf-8"))
    _git(repo, "commit", "-q", "-am", "docs: ours")
    _git(repo, "switch", "-q", "main")
    plan.write_bytes((PLAN + "| 3 | A1 | theirs | x | ⬜ | — | — |\n").encode("utf-8"))
    _git(repo, "commit", "-q", "-am", "docs: theirs")
    _git(repo, "switch", "-q", "topic")
    assert _run(repo, "git", "rebase", "main").returncode != 0
    assert REL in _git(repo, "ls-files", "--unmerged")
    return plan.read_bytes()


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


# --- The hook --------------------------------------------------------------


@pytest.fixture(scope="module")
def tooling(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """This checkout's ``scripts/``, to copy into a repository the hook then runs in."""
    root = tmp_path_factory.mktemp("tooling")
    shutil.copytree(
        REPO_ROOT / "scripts", root / "scripts", ignore=shutil.ignore_patterns("__pycache__")
    )
    return root


def _hooked_repo(repo: Path, tooling: Path) -> Path:
    """A repository the hook can run in, where #2 has just merged and left its row at 🔀.

    #2 lands at 01:30 on 1 November in +1100, which is still 31 October in UTC.
    """
    plan = repo / REL
    plan.parent.mkdir(parents=True)
    shutil.copytree(tooling / "scripts", repo / "scripts")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "chore: tooling")
    assert _run(repo, sys.executable, "scripts/merged_prs.py").returncode == 0
    plan.write_bytes(PLAN.encode("utf-8"))
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "--date", "2026-10-30T12:00:00+11:00", "-m", "docs: the plan (#1)")
    assert _run(repo, sys.executable, "scripts/merged_prs.py").returncode == 0
    _git(repo, "add", HISTORY)
    _git(repo, "commit", "-q", "--date", "2026-11-01T01:30:00+11:00", "-m", "feat: next (#2)")
    return plan


def _staged(repo: Path) -> list[str]:
    return _git(repo, "diff", "--cached", "--name-only").splitlines()


def test_the_next_commit_carries_the_flip_dated_as_the_history_dates_it(
    tmp_path: Path, tooling: Path
) -> None:
    plan = _hooked_repo(tmp_path, tooling)

    proc = _run(tmp_path, sys.executable, "scripts/pre_commit.py", "--skip-fmt")

    assert "✗ plan rows" not in proc.stdout and "✗ plan status" not in proc.stdout
    assert f"| ✅ | [#2]({URL}/2) | 2026-11-01 |" in plan.read_text(encoding="utf-8")
    assert "| 2026-11-01 |" in (tmp_path / HISTORY).read_text(encoding="utf-8")
    assert {REL, HISTORY} <= set(_staged(tmp_path))


def test_the_hook_does_not_sweep_someone_elses_edit_into_the_commit(
    tmp_path: Path, tooling: Path
) -> None:
    """A plan carrying unstaged work keeps it unstaged; only the flip is staged."""
    plan = _hooked_repo(tmp_path, tooling)
    plan.write_text(plan.read_text(encoding="utf-8") + "work in progress\n", encoding="utf-8")

    proc = _run(tmp_path, sys.executable, "scripts/pre_commit.py", "--skip-fmt")

    assert "✗ plan status" not in proc.stdout
    staged = _staged_blob(tmp_path).decode("utf-8")
    assert f"| ✅ | [#2]({URL}/2) | 2026-11-01 |" in staged
    assert "work in progress" not in staged
    text = plan.read_text(encoding="utf-8")
    assert "work in progress\n" in text and f"| ✅ | [#2]({URL}/2) | 2026-11-01 |" in text


def _install_hook(repo: Path) -> None:
    """Run the real hook on ``git commit``; the checks a fixture cannot pass don't refuse it."""
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text(
        f'#!/bin/sh\n"{sys.executable}" scripts/pre_commit.py --skip-fmt\nexit 0\n',
        encoding="utf-8",
    )
    hook.chmod(0o755)


def _row_at_head(repo: Path) -> str:
    return next(ln for ln in _git(repo, "show", f"HEAD:{REL}").splitlines() if "[#2]" in ln)


@pytest.mark.parametrize("flags", [(), ("-a",)], ids=["plain", "commit-all"])
def test_a_plain_commit_carries_the_flip(
    tmp_path: Path, tooling: Path, flags: tuple[str, ...]
) -> None:
    """``-a`` hands the hook a different index file, so it is a commit of its own kind."""
    _hooked_repo(tmp_path, tooling)
    _install_hook(tmp_path)

    _git(tmp_path, "commit", "-q", "--allow-empty", *flags, "-m", "fix: something else")

    assert "| ✅ |" in _row_at_head(tmp_path)


def test_a_commit_of_other_files_carries_the_flip_past_an_unstaged_plan(
    tmp_path: Path, tooling: Path
) -> None:
    """The commit adds the history row, so it must add the flip too, or CI fails it."""
    plan = _hooked_repo(tmp_path, tooling)
    _install_hook(tmp_path)
    plan.write_text(plan.read_text(encoding="utf-8") + "work in progress\n", encoding="utf-8")

    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "fix: something else")

    assert "| ✅ |" in _row_at_head(tmp_path)
    assert "work in progress" not in _git(tmp_path, "show", f"HEAD:{REL}")
    assert "work in progress\n" in plan.read_text(encoding="utf-8")


def test_a_partial_commit_carries_the_flip_and_the_next_commit_keeps_it(
    tmp_path: Path, tooling: Path
) -> None:
    """``git commit <path>`` leaves the old row in the real index, ready to undo the flip."""
    _hooked_repo(tmp_path, tooling)
    _install_hook(tmp_path)
    note = tmp_path / "docs" / "note.md"
    note.write_text("one\n", encoding="utf-8")
    _git(tmp_path, "add", "docs/note.md")
    _git(tmp_path, "commit", "-q", "--no-verify", "-m", "docs: a note")
    note.write_text("two\n", encoding="utf-8")

    _git(tmp_path, "commit", "-q", "-m", "docs: only the note", "docs/note.md")
    assert "| ✅ |" in _row_at_head(tmp_path)

    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "fix: something else")
    assert "| ✅ |" in _row_at_head(tmp_path)


def test_a_shallow_clones_commit_carries_the_history_row_and_the_flip_together(
    tmp_path: Path, tooling: Path
) -> None:
    origin = tmp_path / "origin"
    origin.mkdir()
    _hooked_repo(origin, tooling)
    _git(tmp_path, "clone", "-q", "--depth", "2", origin.as_uri(), "shallow")
    shallow = tmp_path / "shallow"
    _install_hook(shallow)

    _git(shallow, "commit", "-q", "--allow-empty", "-m", "fix: something else")

    assert "[#2](" in _git(shallow, "show", f"HEAD:{HISTORY}")
    assert "| ✅ |" in _row_at_head(shallow)


def test_the_hook_stages_nothing_when_no_row_flips(tmp_path: Path, tooling: Path) -> None:
    plan = _hooked_repo(tmp_path, tooling)
    plan.write_bytes((HEADER + DONE).encode("utf-8"))
    _git(tmp_path, "commit", "-q", "-am", "docs: drop the row")
    other = tmp_path / "docs" / "notes.md"
    other.write_text("unrelated, tracked, and edited\n", encoding="utf-8")
    _git(tmp_path, "add", "docs/notes.md")
    _git(tmp_path, "commit", "-q", "-m", "docs: notes")
    other.write_text("edited again\n", encoding="utf-8")

    _run(tmp_path, sys.executable, "scripts/pre_commit.py", "--skip-fmt")

    assert "docs/notes.md" not in _staged(tmp_path)


def test_the_hook_leaves_a_conflict_unresolved_and_says_why_last(
    tmp_path: Path, tooling: Path
) -> None:
    """``git add`` would mark the conflicted plan resolved, markers and all."""
    plan = _hooked_repo(tmp_path, tooling)
    _git(tmp_path, "add", ".")
    conflicted = _stop_rebase_on_a_conflict(tmp_path, plan)

    proc = _run(tmp_path, sys.executable, "scripts/pre_commit.py", "--skip-fmt")

    assert REL in _git(tmp_path, "ls-files", "--unmerged")
    assert plan.read_bytes() == conflicted
    assert (tmp_path / OTHER).read_text(encoding="utf-8") == PLAN
    assert "skipped plan rows" in proc.stdout.split("Results:", 1)[1]
