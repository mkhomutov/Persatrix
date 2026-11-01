"""Pin how the plan-row writer deals with git itself: the index, line endings, failures, the log.

The writer's rows are pinned in ``test_plan_status_fix.py``. These are the ways
git can make a right row land wrong in the commit: an index written without
the checkout's conversions, a git call that fails or hangs, and a branch log
that reaches back past a stale ``origin/main``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from _git_fixture_helpers import GIT_ENV
from _git_fixture_helpers import git as _git

from scripts import _git as git_module
from scripts._git import git_bytes
from scripts.checks import SKIPPED, plan_row_flip, plan_status
from scripts.checks.plan_status import fix_stale_rows

from ._plan_status_test_helpers import DONE, FLIPPED, HEADER, PLAN, REL
from ._plan_status_test_helpers import plan_repo as _repo
from ._plan_status_test_helpers import staged_blob as _staged_blob

#: How a squash merge made on GitHub is committed.
_GITHUB = {**GIT_ENV, "GIT_COMMITTER_NAME": "GitHub", "GIT_COMMITTER_EMAIL": "noreply@github.com"}


def test_fix_stages_the_flip_with_the_line_endings_the_index_keeps(tmp_path: Path) -> None:
    """With ``core.autocrlf`` the working copy holds CRLF, and the commit must not take it."""
    path = _repo(tmp_path, PLAN, "feat: next (#2)", recorded=(2, 1))
    _git(tmp_path, "config", "core.autocrlf", "true")
    path.unlink()
    _git(tmp_path, "checkout", "--", REL)
    assert b"\r\n" in path.read_bytes()

    assert fix_stale_rows(tmp_path) == 0

    assert _staged_blob(tmp_path) == (HEADER + DONE + FLIPPED).encode("utf-8")


def test_a_merge_github_committed_flips_though_origin_main_lags_behind_it(
    tmp_path: Path,
) -> None:
    """A fork pulls ``main`` from ``upstream``: ``origin/main`` can miss merges the branch has."""
    path = _repo(tmp_path, PLAN, "feat: next (#2)", recorded=(2, 1))
    subprocess.run(
        ["git", "commit", "-q", "--amend", "--no-edit"], cwd=tmp_path, env=_GITHUB, check=True
    )
    _git(tmp_path, "update-ref", "refs/remotes/origin/main", "HEAD~1")
    _git(tmp_path, "switch", "-q", "-c", "topic")

    assert fix_stale_rows(tmp_path) == 0

    assert path.read_text(encoding="utf-8") == HEADER + DONE + FLIPPED


def test_fix_fails_when_it_cannot_stage_its_flip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Else the commit carries the history row with the row still open, and nothing says so."""
    _repo(tmp_path, PLAN, "feat: next (#2)", recorded=(2, 1))
    real = plan_row_flip.git_bytes

    def no_index(root: Path, *args: str, **kwargs: Any) -> bytes | None:
        return None if args[0] == "update-index" else real(root, *args, **kwargs)

    monkeypatch.setattr(plan_row_flip, "git_bytes", no_index)
    capsys.readouterr()

    assert fix_stale_rows(tmp_path) == 1

    assert _staged_blob(tmp_path) == PLAN.encode("utf-8")
    assert "[FAIL]" in capsys.readouterr().out


def test_fix_writes_nothing_when_git_cannot_say_which_plans_have_unstaged_edits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reading a failed ``git diff`` as "no edits" would stage the unstaged work with the flip."""
    path = _repo(tmp_path, PLAN, "feat: next (#2)", recorded=(2, 1))
    path.write_text(PLAN + "work in progress\n", encoding="utf-8")
    real = plan_status.git_output

    def no_diff(root: Path, *args: str) -> str | None:
        return None if args[0] == "diff" else real(root, *args)

    monkeypatch.setattr(plan_status, "git_output", no_diff)

    assert fix_stale_rows(tmp_path) == SKIPPED

    assert path.read_text(encoding="utf-8") == PLAN + "work in progress\n"
    assert _staged_blob(tmp_path) == PLAN.encode("utf-8")


def test_git_bytes_feeds_stdin_and_gives_bytes_or_none(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")

    blob = git_bytes(tmp_path, "hash-object", "--stdin", stdin=b"x\n")

    assert blob == b"587be6b4c3f93f93c489c0111bba5596147a26cb\n"
    assert git_bytes(tmp_path, "show", ":no-such-file") is None


def test_git_bytes_gives_up_on_a_wedged_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stuck index.lock or a credential prompt must not hang the commit."""

    def wedged(argv: list[str], **kwargs: Any) -> None:
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(git_module.subprocess, "run", wedged)

    assert git_bytes(tmp_path, "status") is None
