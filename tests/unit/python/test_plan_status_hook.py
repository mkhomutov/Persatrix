"""Pin how the pre-commit hook carries a merged PR's plan row into the next commit.

Real ``git commit`` calls where it matters: each kind of commit hands the hook
a different index file, and CI judges the commit, not the working copy.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest
from _git_fixture_helpers import git as _git
from _git_fixture_helpers import run as _run

from scripts.merged_prs import REPO_ROOT

from ._plan_status_test_helpers import (
    DONE,
    HEADER,
    HISTORY,
    OTHER,
    PLAN,
    REL,
    URL,
)
from ._plan_status_test_helpers import staged_blob as _staged_blob
from ._plan_status_test_helpers import (
    stop_rebase_on_a_conflict as _stop_rebase_on_a_conflict,
)


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


def test_the_hook_judges_the_plan_the_commit_holds(tmp_path: Path, tooling: Path) -> None:
    """The working copy's row flips; the staged copy's is one the writer leaves for a person."""
    plan = _hooked_repo(tmp_path, tooling)
    plan.write_bytes(PLAN.replace("| 🔀 |", "| 🔀 PR open, rebased |").encode("utf-8"))
    _git(tmp_path, "add", REL)
    plan.write_bytes(PLAN.encode("utf-8"))

    proc = _run(tmp_path, sys.executable, "scripts/pre_commit.py", "--skip-fmt")

    assert f"| ✅ | [#2]({URL}/2) | 2026-11-01 |" in plan.read_text(encoding="utf-8")
    assert "✗ plan status" in proc.stdout


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
