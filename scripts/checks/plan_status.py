#!/usr/bin/env python3
"""Flag progress-table rows that say "PR open" for a PR that has merged.

The most common status-hygiene defect in this repository's history: a PR
merges and the plan row that announced it — ``🔀 PR open ([#855](…))`` — is
never flipped to ``✅ Merged``. Every open cycle's master plan and
release-prep plan, plus every RFC and issue-owned PR plan, carries such a
table. This check reads those tables and reports a row whose status cell
starts with 🔀 (PR open) or ⬜ (not started) while **every** PR it links to
is already a squash-merge subject on ``main``.

A PR usually writes its own row before its number exists, so that row links
nothing — ``🔀 PR open | — (this PR) |``. Such a row is judged by the commit
that wrote it instead: once that commit is a squash-merge subject, the PR
has merged and the row is stale. While the PR is open the line comes from a
branch commit or the working tree, so the row passes.

🔄 (in progress) cells are left alone: they legitimately mix merged and open
PRs ("PR 0 merged (#854), PR 1 open (#855)"). Released versions' plans are
frozen evidence and are skipped; which versions are released is read from
``CHANGELOG.md`` the same way the size checker does.

Usage::

    python scripts/checks/plan_status.py [--verbose]

Exit code: 0 clean, 1 if any stale row.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts._git import git_output  # noqa: E402
from scripts.checks import ensure_utf8_stdout  # noqa: E402
from scripts.checks.released import is_released_version_doc, released_versions  # noqa: E402

_PR_LINK_RE = re.compile(r"\[#(\d+)\]\(")
_LINK_ONLY_CELL_RE = re.compile(r"^\[#(\d+)\]\([^)]+\)$")
_SUBJECT_RE = re.compile(r"\(#(\d+)\)$")
_STALE_LEADERS = ("🔀", "⬜")


class StaleRow(NamedTuple):
    file: str
    line: int
    merged: tuple[int, ...]
    cell: str
    #: The row links no PR; *merged* names the PR whose squash-merge wrote it.
    unlinked: bool = False


def merged_pr_numbers(repo_root: Path) -> frozenset[int]:
    """PR numbers squash-merged on the history reachable from HEAD."""
    log = git_output(repo_root, "log", "--first-parent", "--format=%s")
    if log is None:
        return frozenset()
    numbers = set()
    for subject in log.splitlines():
        m = _SUBJECT_RE.search(subject.strip())
        if m:
            numbers.add(int(m.group(1)))
    return frozenset(numbers)


def pr_that_wrote(repo_root: Path, rel: str, lineno: int) -> int | None:
    """The PR whose squash-merge last wrote line *lineno* of *rel*, or ``None``.

    ``git blame --first-parent`` walks the same history as
    ``merged_pr_numbers``. A line from the working tree or a branch commit
    has no PR: that is a PR's own row while the PR is still open. Nor does a
    boundary commit — a shallow clone's oldest commit, or the root — which
    blame credits with every older line, whoever wrote it.
    """
    out = git_output(
        repo_root, "blame", "--first-parent", "--line-porcelain",
        "-L", f"{lineno},{lineno}", "--", rel,
    )
    if out is None:
        return None
    fields = out.split("\n")
    if "boundary" in fields:
        return None
    summary = next((f.removeprefix("summary ") for f in fields if f.startswith("summary ")), "")
    m = _SUBJECT_RE.search(summary.strip())
    return int(m.group(1)) if m else None


def _split_row(line: str) -> list[str]:
    inner = line.strip()
    if not (inner.startswith("|") and inner.endswith("|")):
        return []
    return [c.strip() for c in inner[1:-1].split("|")]


def find_stale_rows(
    text: str,
    merged: frozenset[int],
    written_by: Callable[[int], int | None] | None = None,
) -> list[StaleRow]:
    """Rows whose 🔀 / ⬜ status cell announces PRs that have all merged.

    For a 🔀 row every PR link on the row counts (the status cell names the
    open PR, or a dedicated "GitHub PR" column does). A 🔀 row that links
    nothing is a PR's own row: *written_by* maps its line number to the PR
    whose squash-merge wrote it, and that PR counts. For a ⬜ row only a cell
    that is *nothing but* a PR link counts — a not-started row that merely
    cites a merged PR in prose ("after #844 lands") is not stale.
    """
    stale: list[StaleRow] = []
    # Split on "\n" alone, as git does, so line n here is line n to git blame.
    for lineno, line in enumerate(text.split("\n"), start=1):
        cells = _split_row(line)
        if not cells:
            continue
        status = next((c for c in cells if c.startswith(_STALE_LEADERS)), None)
        if status is None:
            continue
        writer: int | None = None
        if status.startswith("🔀"):
            linked = {int(n) for n in _PR_LINK_RE.findall(line)}
            if not linked and written_by is not None:
                writer = written_by(lineno)
                linked = set() if writer is None else {writer}
        else:
            linked = {
                int(m.group(1)) for c in cells if (m := _LINK_ONLY_CELL_RE.match(c))
            }
        if not linked or not linked <= merged:
            continue
        stale.append(StaleRow("", lineno, tuple(sorted(linked)), status[:60], writer is not None))
    return stale


def target_docs(repo_root: Path) -> list[Path]:
    """Open-cycle plans + every RFC / issue PR plan."""
    released = released_versions(repo_root)
    docs: list[Path] = []
    # ``v*-plan.md`` also matches ``v*-release-prep-plan.md`` and the two
    # ``v*-test-findings-pr-plan.md`` files; one glob, deduped, frozen ones out.
    for p in sorted(set((repo_root / "docs").glob("v*-plan.md"))):
        rel = p.relative_to(repo_root).as_posix()
        if not is_released_version_doc(rel, released):
            docs.append(p)
    docs += sorted((repo_root / "docs" / "rfcs").glob("*pr-plan*.md"))
    docs += sorted((repo_root / "docs" / "issues").glob("*pr-plan*.md"))
    return docs


def check_plan_status(repo_root: Path, verbose: bool = False) -> int:
    merged = merged_pr_numbers(repo_root)
    if not merged:
        print("[WARN] no squash-merge subjects found in git log — skipping plan-status check")
        return 0
    stale: list[StaleRow] = []
    docs = target_docs(repo_root)
    for p in docs:
        rel = p.relative_to(repo_root).as_posix()
        # Not read_text(): it turns a lone "\r" into a line end git blame does not count.
        text = p.read_bytes().decode("utf-8", errors="replace")
        written_by = partial(pr_that_wrote, repo_root, rel)
        stale += [s._replace(file=rel) for s in find_stale_rows(text, merged, written_by)]
        if verbose:
            print(f"  scanned {rel}")
    print(f"[SCAN] Checked progress tables in {len(docs)} plan(s) against {len(merged)} merged PRs")
    if stale:
        print(f"\n[FAIL] {len(stale)} stale row(s) — the PR merged but the row still says open:")
        for s in stale:
            prs = ", ".join(f"#{n}" for n in s.merged)
            if s.unlinked:
                print(f"  {s.file}:{s.line}: {s.cell!r} links no PR; {prs} wrote it and has"
                      " merged — flip the row and link its PR")
            else:
                print(f"  {s.file}:{s.line}: {s.cell!r} links {prs}, all merged — flip the row")
        return 1
    print("[OK] No plan row announces an open PR that has already merged.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ensure_utf8_stdout()
    parser = argparse.ArgumentParser(description="Flag stale 'PR open' rows in plan tables.")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    return check_plan_status(REPO_ROOT, verbose=args.verbose)


if __name__ == "__main__":
    raise SystemExit(main())
