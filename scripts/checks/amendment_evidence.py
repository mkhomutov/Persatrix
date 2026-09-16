#!/usr/bin/env python3
"""Fail when a sequencing amendment has no filled-in External evidence section.

A sequencing amendment decides which work goes into which release. From the
Amendment 2026-09-12 on, each one must first record what happened outside the
project since the last one: installs by anyone other than the author, issues
or pull requests from anyone else, demos shown, user conversations written
up, experiment results published (docs/methodology/decisions.md rule 6). When
every row says "none", the amendment may not scope a release on internal
correctness alone. That ruling asked for a check rather than trusting
intention, and this is it.

It reads every ``docs/v*-sequencing.md`` and, for each ``## Amendment
YYYY-MM-DD — …`` section dated on or after the rule (2026-09-12), looks for a
heading starting "External evidence since the last amendment" and, under it,
a table with one row per required field. A missing section, a missing row, or
a row whose value is blank or still a template placeholder fails. "none" is a
value: an empty result is still evidence, and rule 6 is what reads it.

Earlier amendments are not judged: rule 1 keeps them verbatim, so they never
had the section. Whether the amendment then obeys rule 6 or rule 7 is a
judgement a reviewer makes; this only makes sure the evidence is on the page.

Usage::

    python scripts/checks/amendment_evidence.py

Exit code: 0 clean; 1 if an amendment's section is missing or incomplete, or
if no amendment dated on or after the rule was found — a renamed sequencing
doc must not leave a check that reads nothing and passes.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.checks import ensure_utf8_stdout  # noqa: E402

DOCS_DIR = REPO_ROOT / "docs"
SEQUENCING_GLOB = "v*-sequencing.md"

# The date of the amendment that made the section a rule.
RULE_FROM = date(2026, 9, 12)
SECTION_TITLE = "External evidence since the last amendment"
# The rows of the section, in the order docs/templates/PLAN_AMENDMENT_TEMPLATE.md
# lists them; a unit test keeps the two equal.
REQUIRED_FIELDS = (
    "Installs by anyone other than the author",
    "Issues or pull requests from anyone else",
    "Demos shown, to whom, when",
    "User conversations written up",
    "Experiment results published",
)

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_AMENDMENT_RE = re.compile(r"^Amendment (\d{4}-\d{2}-\d{2})\b")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_PLACEHOLDER_RE = re.compile(r"^(<.*>|…|\.\.\.)$")


class Amendment(NamedTuple):
    line: int
    date: date
    title: str
    body: tuple[str, ...]  # the lines under the heading, up to the next level-1 or -2 heading


def _headings(lines: list[str]) -> Iterator[tuple[int, int, str]]:
    """(index, level, text) of every heading outside a code fence."""
    fence: str | None = None
    for i, line in enumerate(lines):
        opened = _FENCE_RE.match(line)
        if opened:
            if fence is None:
                fence = opened.group(1)
            elif fence == opened.group(1):
                fence = None
            continue
        if fence is not None:
            continue
        m = _HEADING_RE.match(line)
        if m:
            yield i, len(m.group(1)), m.group(2)


def amendments(text: str) -> list[Amendment]:
    """Every dated ``## Amendment`` section of a sequencing document."""
    lines = text.splitlines()
    top = [(i, t) for i, level, t in _headings(lines) if level <= 2]
    found: list[Amendment] = []
    for n, (i, title) in enumerate(top):
        m = _AMENDMENT_RE.match(title)
        if not m:
            continue
        end = top[n + 1][0] if n + 1 < len(top) else len(lines)
        found.append(
            Amendment(i + 1, date.fromisoformat(m.group(1)), title, tuple(lines[i + 1 : end]))
        )
    return found


def _label(cell: str) -> str:
    return " ".join(cell.replace("*", "").split()).lower()


def _cells(line: str) -> list[str] | None:
    inner = line.strip()
    if not (inner.startswith("|") and inner.endswith("|") and len(inner) > 1):
        return None
    return [c.strip() for c in inner[1:-1].split("|")]


def evidence_rows(text: str) -> dict[str, str] | None:
    """The External evidence table as {lower-cased label: value}, or None with no section.

    The section runs from its heading to the next heading of the same or a
    higher level. Each table's first row is its header and is skipped, as is
    the ``|---|`` divider under it.
    """
    lines = text.splitlines()
    heads = list(_headings(lines))
    for n, (i, level, title) in enumerate(heads):
        if not title.lower().startswith(SECTION_TITLE.lower()):
            continue
        end = next((j for j, lv, _ in heads[n + 1 :] if lv <= level), len(lines))
        return _table_rows(lines[i + 1 : end])
    return None


def _table_rows(lines: list[str]) -> dict[str, str]:
    rows: dict[str, str] = {}
    in_table = False
    for line in lines:
        cells = _cells(line)
        if cells is None:
            in_table = False
            continue
        if not in_table:
            in_table = True  # the header row
            continue
        if all(not c.strip("-: ") for c in cells):
            continue  # the divider
        rows[_label(cells[0])] = cells[1] if len(cells) > 1 else ""
    return rows


def evidence_problems(amendment: Amendment) -> list[str]:
    """What the amendment's External evidence section lacks; empty when complete."""
    rows = evidence_rows("\n".join(amendment.body))
    if rows is None:
        return [f"no {SECTION_TITLE} section"]
    problems: list[str] = []
    for field in REQUIRED_FIELDS:
        value = rows.get(field.lower())
        if value is None:
            problems.append(f"no row for {field!r}")
        elif not value.strip() or _PLACEHOLDER_RE.match(value.strip()):
            problems.append(f"{field!r} is blank")
    return problems


def main(argv: list[str] | None = None) -> int:
    ensure_utf8_stdout()
    argparse.ArgumentParser(
        description="Fail when a sequencing amendment has no filled-in External evidence section.",
    ).parse_args(argv)
    docs = sorted(DOCS_DIR.glob(SEQUENCING_GLOB))
    judged = 0
    failures: list[str] = []
    for doc in docs:
        rel = doc.relative_to(DOCS_DIR.parent)
        for a in amendments(doc.read_text(encoding="utf-8")):
            if a.date < RULE_FROM:
                continue
            judged += 1
            failures.extend(f"  {rel}:{a.line}: {a.title} — {p}" for p in evidence_problems(a))
    since = RULE_FROM.isoformat()
    if not judged:
        print(
            f"[FAIL] Found no amendment dated {since} or later in"
            f" {DOCS_DIR.name}/{SEQUENCING_GLOB}, so this check read nothing —"
            " if the sequencing record moved, point"
            " scripts/checks/amendment_evidence.py at it."
        )
        return 1
    print(
        f"[SCAN] Checked {judged} amendment(s) dated {since} or later"
        f" in {len(docs)} sequencing doc(s)"
    )
    if failures:
        print(
            f"\n[FAIL] {len(failures)} gap(s) in External evidence sections"
            " (docs/methodology/decisions.md rule 6; fill each row, 'none' if nothing happened):"
        )
        print("\n".join(failures))
        return 1
    print(f"[OK] Every amendment since {since} records its external evidence.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
