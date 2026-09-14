#!/usr/bin/env python3
"""Flag a ROADMAP Component Status row that is behind the RFC it names.

ROADMAP.md's "Component Status" tables give each package or module a Status
cell, and most of those cells name the RFC that built it: "✅ Complete
(RFC 0006 PR 1a)". Nothing compared the cell with the RFC, so the
``internal/security/`` row said "🚧 In progress (v0.3.0 — RFC 0009 PRs
1/1b/1c/2/3)" from May to September 2026 while RFC 0009's front-matter said
``partially_implemented``.

For each row, this reads the front-matter ``status:`` of every ``RFC NNNN``
its Status cell names — the files ``scripts/rfcs.py`` indexes — and flags the
row when it says less than all of them:

- every named RFC ``implemented`` (or ``stable``): the row must not start with
  ⚠️, 🚧, 🔲, 📋, 🔮 or ⬜ — a finished RFC leaves nothing half-built;
- every named RFC shipped at least in part (``partially_implemented`` too):
  the row must not start with 🚧 — none of that work is in progress. A part
  moved to a later version is 🔲; a finished part is ✅.

An RFC that is still ``implementing``, has not started, or has no file yet
sets no limit: a component can be finished before its RFC is, and a row may
mention the RFC that will extend it later. Only the Status cell is read, so an
RFC mentioned in the Purpose cell for context is not compared. RFC Scope,
Planned Components and Version Map tables are not read: their rows describe
one phase of an RFC, which can rightly differ from the RFC's own status.

The check knows ROADMAP.md's layout, so docs/methodology/conformance.json lists
it under ``persatrix_specific`` rather than with the portable tooling.

Usage::

    python scripts/checks/roadmap_status.py

Exit code: 0 clean; 1 if a row is behind its RFC, or if ROADMAP.md has no
Component Status table — a renamed heading must not leave a check that reads
nothing and passes.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import rfcs  # noqa: E402
from scripts.checks import ensure_utf8_stdout  # noqa: E402

ROADMAP_FILE = REPO_ROOT / "ROADMAP.md"

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_RFC_REF_RE = re.compile(r"\bRFC[ -](\d{4})\b")
_RFC_ID_RE = re.compile(r"^RFC-(\d{4})$")

_FINISHED = frozenset({"implemented", "stable"})
_SHIPPED = _FINISHED | {"partially_implemented"}
# Leading markers of a component that is not finished: partial ("⚠" with or
# without its U+FE0F), in progress, a stub, planned or proposed, future, and
# not started.
_UNFINISHED = ("⚠", "🚧", "🔲", "📋", "🔮", "⬜")
_IN_PROGRESS = "🚧"


class StatusRow(NamedTuple):
    line: int
    component: str
    status: str


class BehindRow(NamedTuple):
    line: int
    component: str
    status: str
    rfcs: tuple[tuple[str, str], ...]  # (number, front-matter status) per RFC the cell names
    fix: str


def _cells(line: str) -> list[str]:
    inner = line.strip()
    if not (inner.startswith("|") and inner.endswith("|")):
        return []
    return [c.strip() for c in inner[1:-1].split("|")]


def component_status_rows(text: str) -> list[StatusRow]:
    """Every data row of a table under a ``Component Status`` heading.

    The section ends at the next heading of level 3 or above; its ``####``
    sub-headings (Go Orchestrator, Python Agents, …) stay inside it. The
    Status column is found by its header, so a table without one is skipped.
    """
    rows: list[StatusRow] = []
    in_section = False
    status_col: int | None = None  # None until the current table's header is read
    for lineno, line in enumerate(text.splitlines(), start=1):
        heading = _HEADING_RE.match(line)
        if heading:
            if len(heading.group(1)) <= 3:
                in_section = heading.group(2).startswith("Component Status")
            status_col = None
            continue
        cells = _cells(line)
        if not cells:
            status_col = None  # a table ends at the first line that is not a row
            continue
        if not in_section:
            continue
        if status_col is None:
            status_col = cells.index("Status") if "Status" in cells else -1
            continue
        if not 0 <= status_col < len(cells) or not cells[status_col].strip("-: "):
            continue  # no Status column, a short row, or the |---| divider
        rows.append(StatusRow(lineno, cells[0], cells[status_col]))
    return rows


def find_behind_rows(rows: Iterable[StatusRow], statuses: Mapping[str, str]) -> list[BehindRow]:
    """The rows that say less than every RFC their Status cell names.

    ``statuses`` maps an RFC number ("0009") to its front-matter status. An
    RFC with no file yet ("RFC 0010, not yet written") counts as not started.
    """
    behind: list[BehindRow] = []
    for row in rows:
        named = {n: statuses.get(n, "") for n in _RFC_REF_RE.findall(row.status)}
        if not named:
            continue
        fix = _fix(row.status, set(named.values()))
        if fix:
            behind.append(
                BehindRow(row.line, row.component, row.status, tuple(named.items()), fix)
            )
    return behind


def _fix(cell: str, named: set[str]) -> str | None:
    """How the cell must change to keep up with the ``named`` statuses, or None."""
    if named <= _FINISHED and cell.startswith(_UNFINISHED):
        return "mark the row ✅"
    if named <= _SHIPPED and cell.startswith(_IN_PROGRESS):
        return "none of it is in progress; use ✅, ⚠️ or 🔲"
    return None


def rfc_statuses() -> dict[str, str]:
    """RFC number → front-matter status, from the files ``scripts/rfcs.py`` indexes."""
    collected, _ = rfcs.collect_rfcs()  # `make rfcs-check` reports any it cannot read
    return {m.group(1): r.status for r in collected if (m := _RFC_ID_RE.match(r.id))}


def main(argv: list[str] | None = None) -> int:
    ensure_utf8_stdout()
    argparse.ArgumentParser(
        description="Flag a ROADMAP Component Status row that is behind the RFC it names.",
    ).parse_args(argv)
    name = ROADMAP_FILE.name
    rows = component_status_rows(ROADMAP_FILE.read_text(encoding="utf-8"))
    if not rows:
        print(
            f"[FAIL] {name} has no Component Status table, so this check read nothing —"
            " if the tables moved, point scripts/checks/roadmap_status.py at them."
        )
        return 1
    behind = find_behind_rows(rows, rfc_statuses())
    print(f"[SCAN] Checked {len(rows)} Component Status row(s) in {name} against their RFCs")
    if behind:
        print(f"\n[FAIL] {len(behind)} row(s) say less than the RFC they name:")
        for b in behind:
            said = ", ".join(f"RFC {n} is {s}" for n, s in b.rfcs)
            cell = b.status if len(b.status) <= 60 else b.status[:59] + "…"
            print(f"  {name}:{b.line}: {b.component} says {cell!r}, but {said} — {b.fix}")
        return 1
    print("[OK] Every Component Status row keeps up with the RFC it names.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
