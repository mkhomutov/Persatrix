"""A repository for the plan-row tests: the plan, its merges and the history that records them.

Shared by ``test_plan_status_fix.py``, which runs the writer, and
``test_plan_status_hook.py``, which runs it through a real commit.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from _git_fixture_helpers import GIT_ENV
from _git_fixture_helpers import git as _git
from _git_fixture_helpers import run as _run

from scripts.merged_prs import MergedPR, render

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
OTHER = "docs/rfcs/0098-pr-plan.md"


def plan_repo(repo: Path, plan: str, *merges: str, recorded: tuple[int, ...] = (1,)) -> Path:
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


def staged_blob(repo: Path) -> bytes:
    """The plan as the next commit would hold it, byte for byte."""
    proc = subprocess.run(
        ["git", "show", f":{REL}"], cwd=repo, capture_output=True, check=True, env=GIT_ENV
    )
    return proc.stdout


def stop_rebase_on_a_conflict(repo: Path, plan: Path) -> bytes:
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
