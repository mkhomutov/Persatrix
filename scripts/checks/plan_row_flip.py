"""Turn an open plan row into a merged one, and put it in the commit.

The hands of ``plan_status.py --fix``. That script decides which rows have
merged. This module knows the shapes a row comes in, rewrites one only when
nothing in it would be lost or made up, and writes a plan's staged copy without
touching the working copy. Any other row stays for a person.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import NamedTuple

from scripts._git import git_output
from scripts.checks import markdown_page
from scripts.merged_prs import pr_link

PR_LINK_RE = re.compile(r"\[#(\d+)\]\(")
STALE_LEADERS = ("🔀", "⬜")
_LINK_RE = re.compile(r"\[#\d+\]\([^)]+\)")
#: What a status cell may say, its links aside, for the writer to replace it:
#: nothing a flip would lose.
_PLAIN_STATUSES = frozenset(
    {"🔀", "⬜", "🔀 open", "🔀 PR open", "🔀 PR open (this PR)", "⬜ Not started"}
)
_WORDLESS_STATUSES = frozenset({"🔀", "⬜", "🔀 open"})
#: A PR or Merged cell with nothing in it yet.
_PLACEHOLDER_RE = re.compile(r"^(—|-)?\s*(\(this PR\)|_this PR_)?$")
_PR_COLUMN = "GitHub PR"
_MERGED_COLUMN = "Merged"
_STATUS_COLUMN = "Status"


class StaleRow(NamedTuple):
    file: str
    line: int
    merged: tuple[int, ...]
    cell: str
    #: The row links no PR; *merged* names the PR whose squash-merge wrote it.
    unlinked: bool = False

    @property
    def prs(self) -> str:
        return ", ".join(f"#{n}" for n in self.merged)


def _header_of(shown: Sequence[str], index: int) -> list[str]:
    """The header row of the table that holds line *index* of the rendered page."""
    while index > 0 and markdown_page.cells(shown[index - 1]) is not None:
        index -= 1
    return markdown_page.cells(shown[index]) or []


def flipped_row(
    line: str, header: Sequence[str], row: StaleRow, dates: Mapping[int, str],
) -> str | None:
    """*line* with its PR marked merged, or ``None`` if that could lose or invent something.

    The link and the date go in their own columns, or in the status cell of a
    table without them. ``None`` for a status that says more than its marker,
    a link outside the PR's own cell (it may only cite a PR), or a cell that
    already holds something else: those need a person.
    """
    cells = markdown_page.cells(line)
    if cells is None or len(cells) != len(header):
        return None
    status_at = next(i for i, cell in enumerate(cells) if cell.startswith(STALE_LEADERS))
    bare = " ".join(_LINK_RE.sub("", cells[status_at]).split())
    if header[status_at] != _STATUS_COLUMN or bare not in _PLAIN_STATUSES:
        return None
    pr_at = header.index(_PR_COLUMN) if _PR_COLUMN in header else status_at
    date_at = header.index(_MERGED_COLUMN) if _MERGED_COLUMN in header else status_at
    if row.unlinked:
        placed = pr_at == status_at or _PLACEHOLDER_RE.match(cells[pr_at]) is not None
    else:
        placed = {int(n) for n in PR_LINK_RE.findall(cells[pr_at])} == set(row.merged)
    if not placed or (date_at != status_at and not _PLACEHOLDER_RE.match(cells[date_at])):
        return None

    links = ", ".join(pr_link(str(n)) for n in row.merged)
    date = max(dates[n] for n in row.merged)
    status = ["✅" if bare in _WORDLESS_STATUSES else "✅ Merged"]
    new: dict[int, str] = {}
    if pr_at == status_at:
        status.append(links)
    elif row.unlinked:
        new[pr_at] = links
    if date_at == status_at:
        status.append(date)
    else:
        new[date_at] = date
    new[status_at] = " ".join(status)
    return markdown_page.with_cells(line, new)


def flip_rows(
    text: str, stale: Sequence[StaleRow], dates: Mapping[int, str],
) -> tuple[str, list[StaleRow]]:
    """*text* with each row of *stale* flipped, and the rows left for a person."""
    lines = text.split("\n")
    shown = markdown_page.rendered(lines)
    left: list[StaleRow] = []
    for row in stale:
        at = row.line - 1
        flipped = None
        if dates.keys() >= set(row.merged):
            flipped = flipped_row(lines[at], _header_of(shown, at), row, dates)
        if flipped is None:
            left.append(row)
        else:
            lines[at] = flipped
    return "\n".join(lines), left


def _git_bytes(repo_root: Path, *args: str, stdin: bytes | None = None) -> bytes | None:
    """``git <args>`` stdout as bytes, or ``None`` on failure: a plan's bytes must survive."""
    proc = subprocess.run(
        ["git", *args], cwd=repo_root, input=stdin, capture_output=True, check=False,
    )
    return proc.stdout if proc.returncode == 0 else None


def staged_bytes(repo_root: Path, rel: str) -> bytes:
    """The staged copy of *rel*, empty if there is none."""
    return _git_bytes(repo_root, "show", f":{rel}") or b""


def stage(repo_root: Path, rel: str, text: str) -> None:
    """Make *text* the staged copy of *rel*, without touching the working copy.

    Not ``git add``: the working copy may hold edits that are not this commit's.
    A file git does not track stays untracked.
    """
    entry = git_output(repo_root, "ls-files", "--stage", "--", rel)
    if not entry:
        return
    blob = _git_bytes(
        repo_root, "hash-object", "-w", "--stdin", stdin=text.encode("utf-8", "surrogateescape"),
    )
    if blob is not None:
        info = f"{entry.split()[0]},{blob.decode().strip()},{rel}"
        _git_bytes(repo_root, "update-index", "--cacheinfo", info)
