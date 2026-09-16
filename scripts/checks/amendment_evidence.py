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

It reads every Markdown file under ``docs/`` with "sequencing" in its path
(the gitignored ``docs/pr-reviews/`` aside) and, for each ``## Amendment
YYYY-MM-DD — …`` section dated on or after the rule (2026-09-12), looks for a
heading starting "External evidence since the last amendment" and, under it,
a rendered table with one row per required field. A missing section, a
missing row, a row given twice, or a row whose value is blank, a template
placeholder or "TBD" fails. "none" is a value: an empty result is still
evidence, and rule 6 is what reads it. A heading from that date on that names
an amendment in any other form (another case, level or word order), or a
date that does not exist, fails too: skipping it would leave the newest
amendment unread while the 2026-09-12 one keeps the check green.

The page is read as GitHub shows it (scripts/checks/markdown_page.py): a
heading or a table inside a code fence or an HTML comment does not count, and
a fence or comment that never closes fails, since everything after it would
go unread.

Earlier amendments are not judged: rule 1 keeps them verbatim, so they never
had the section. Whether the amendment then obeys rule 6 or rule 7 is a
judgement a reviewer makes; this only makes sure the evidence is on the page.

The check knows the sequencing record's layout and the date the rule was
ratified, so docs/methodology/conformance.json lists it under
``persatrix_specific`` rather than with the portable tooling.

Usage::

    python scripts/checks/amendment_evidence.py

Exit code: 0 clean; 1 if an amendment's section is missing or incomplete, if
a heading names an amendment the check cannot read, if a code fence or HTML
comment never closes, or if no amendment dated on or after the rule was found
— a renamed sequencing doc must not leave a check that reads nothing and
passes.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterator, Sequence
from datetime import date
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.checks import ensure_utf8_stdout, markdown_page  # noqa: E402

DOCS_DIR = REPO_ROOT / "docs"
# Local-only PR review reports quote amendments; they are not the record.
LOCAL_ONLY_DIR = "pr-reviews"

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
HEADING_FORM = "`## Amendment YYYY-MM-DD — <title>`"

_AMENDMENT_RE = re.compile(r"^Amendment (\d{4}-\d{2}-\d{2})\b")
# A heading that names a dated amendment in any shape: another case or word order.
_ANY_AMENDMENT_RE = re.compile(
    r"^(?:amendment\b\W*(\d{4}-\d{1,2}-\d{1,2})|(\d{4}-\d{1,2}-\d{1,2})\W*amendment\b)",
    re.IGNORECASE,
)
# "<fill in>", but not an autolink ("<https://…>"), an e-mail or an HTML tag.
_PLACEHOLDER_RE = re.compile(r"^<(?![a-z][a-z0-9+.-]*:)[^<>=@]*>$", re.IGNORECASE)
_DEFERRED_RE = re.compile(r"^(?:tbd|tba|todo|fixme|xxx)\b", re.IGNORECASE)


class Amendment(NamedTuple):
    line: int
    date: date
    title: str
    body: tuple[str, ...]  # the lines under the heading, up to the next level-1 or -2 heading


def sequencing_docs(docs_dir: Path) -> list[Path]:
    """Every Markdown file under *docs_dir* with "sequencing" in its path."""
    found: list[Path] = []
    for path in sorted(docs_dir.rglob("*.md")):
        parts = path.relative_to(docs_dir).parts
        if parts[0] != LOCAL_ONLY_DIR and any("sequencing" in p.lower() for p in parts):
            found.append(path)
    return found


def _plain(text: str) -> str:
    """*text* without emphasis markers or repeated spaces."""
    return " ".join(re.sub(r"[*_]", "", text).split())


def _headings(lines: Sequence[str]) -> Iterator[tuple[int, int, str]]:
    """(index, level, text without emphasis) of every heading the page shows."""
    for i, level, text in markdown_page.headings(lines):
        yield i, level, _plain(text)


def _is_section(title: str) -> bool:
    return title.lower().startswith(SECTION_TITLE.lower())


def amendments(text: str) -> list[Amendment]:
    """Every ``## Amendment`` section of a sequencing document with a real date.

    A section ends at the next level-1 or -2 heading — except an External
    evidence heading, which the template writes at level 2 and which still
    belongs to the amendment above it.
    """
    lines = text.splitlines()
    top = [(i, t) for i, level, t in _headings(lines) if level <= 2 and not _is_section(t)]
    found: list[Amendment] = []
    for n, (i, title) in enumerate(top):
        m = _AMENDMENT_RE.match(title)
        if not m:
            continue
        try:
            when = date.fromisoformat(m.group(1))
        except ValueError:
            continue  # heading_problems reports it
        end = top[n + 1][0] if n + 1 < len(top) else len(lines)
        found.append(Amendment(i + 1, when, title, tuple(lines[i + 1 : end])))
    return found


def heading_problems(text: str) -> list[tuple[int, str, str]]:
    """(line, title, problem) for each amendment heading the check cannot read.

    That is a heading naming an amendment whose date does not exist, or one
    dated from the rule on that is not in the one form ``amendments`` reads.
    """
    problems: list[tuple[int, str, str]] = []
    for i, level, title in _headings(text.splitlines()):
        m = _ANY_AMENDMENT_RE.match(title)
        if not m:
            continue
        raw = m.group(1) or m.group(2)
        try:
            when = date.fromisoformat(raw)
        except ValueError:
            problems.append((i + 1, title, f"{raw} is not a date (write YYYY-MM-DD)"))
            continue
        if when >= RULE_FROM and not (level == 2 and _AMENDMENT_RE.match(title)):
            problems.append(
                (i + 1, title, f"not in the {HEADING_FORM} form, so its evidence goes unread")
            )
    return problems


def evidence_rows(lines: Sequence[str]) -> dict[str, list[str]] | None:
    """The External evidence table as {lower-cased label: [value, …]}, or None with no section.

    The section runs from its heading to the next heading of the same or a
    higher level. Only a rendered table counts — a header row with a
    ``|---|`` divider under it, then its body rows — so a table inside a code
    fence or an HTML comment fills nothing.
    """
    heads = list(_headings(lines))
    for n, (i, level, title) in enumerate(heads):
        if not _is_section(title):
            continue
        end = next((j for j, lv, _ in heads[n + 1 :] if lv <= level), len(lines))
        return _table_rows(markdown_page.rendered(lines)[i + 1 : end])
    return None


def _table_rows(lines: list[str]) -> dict[str, list[str]]:
    rows: dict[str, list[str]] = {}
    in_table = False
    for n, line in enumerate(lines):
        cells = markdown_page.cells(line)
        if cells is None:
            in_table = False
        elif markdown_page.is_divider(cells):
            continue
        elif in_table:
            value = cells[1] if len(cells) > 1 else ""
            rows.setdefault(_plain(cells[0]).lower(), []).append(value)
        else:  # a header row starts a table only with a divider under it
            under = markdown_page.cells(lines[n + 1]) if n + 1 < len(lines) else None
            in_table = under is not None and markdown_page.is_divider(under)
    return rows


def _is_blank(value: str) -> bool:
    """A value that records nothing: no letter or digit, a placeholder, or TBD."""
    text = value.strip()
    return (
        not re.search(r"[^\W_]", text)
        or bool(_PLACEHOLDER_RE.match(text))
        or bool(_DEFERRED_RE.match(_plain(text)))
    )


def evidence_problems(amendment: Amendment) -> list[str]:
    """What the amendment's External evidence section lacks; empty when complete."""
    rows = evidence_rows(amendment.body)
    if rows is None:
        return [f"no {SECTION_TITLE} section"]
    problems: list[str] = []
    for field in REQUIRED_FIELDS:
        values = rows.get(field.lower())
        if not values:
            problems.append(f"no row for {field!r}")
        elif len(values) > 1:
            problems.append(f"{field!r} has {len(values)} rows; keep one")
        elif _is_blank(values[0]):
            problems.append(f"{field!r} is blank")
    return problems


def main(argv: list[str] | None = None) -> int:
    ensure_utf8_stdout()
    argparse.ArgumentParser(
        description="Fail when a sequencing amendment has no filled-in External evidence section.",
    ).parse_args(argv)
    docs = sequencing_docs(DOCS_DIR)
    judged = 0
    failures: list[str] = []
    for doc in docs:
        rel = doc.relative_to(DOCS_DIR.parent).as_posix()
        text = doc.read_text(encoding="utf-8")
        opened = markdown_page.unclosed(text.splitlines())
        if opened is not None:
            failures.append(
                f"  {rel}:{opened + 1}: a code fence or HTML comment opened here never closes,"
                " so nothing after it is read — close it"
            )
        failures.extend(f"  {rel}:{n}: {title} — {p}" for n, title, p in heading_problems(text))
        for a in amendments(text):
            if a.date < RULE_FROM:
                continue
            judged += 1
            failures.extend(f"  {rel}:{a.line}: {a.title} — {p}" for p in evidence_problems(a))
    since = RULE_FROM.isoformat()
    if not judged:
        print(
            f"[FAIL] Found no amendment dated {since} or later in a {DOCS_DIR.name}/ file"
            " with 'sequencing' in its path, so this check read nothing — if the"
            " sequencing record moved, point scripts/checks/amendment_evidence.py at it."
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
