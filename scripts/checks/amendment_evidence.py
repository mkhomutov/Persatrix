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

It reads every tracked ``docs/**/v*-sequencing*.md`` and, for each
``## Amendment YYYY-MM-DD — …`` section dated on or after the rule
(2026-09-12), looks for a heading starting "External evidence since the last
amendment" and, under it, a rendered table with one row per required field.
The sections the amendment template writes at level 2 stay in the amendment.
A missing section, a missing row, a row given twice, or a row whose value is
blank, a template placeholder or "TBD" fails. "none" is a value: an empty
result is still evidence, and rule 6 is what reads it. A heading from that
date on that names an amendment in any other form (another level, case, word
order or date form, extra words, markup, or a setext heading), or a date that
does not exist, fails too: skipping it would leave the newest amendment
unread while the 2026-09-12 one keeps the check green. A sub-heading of an
amendment that names one no newer than it is a reference, not a new one.

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
from fnmatch import fnmatchcase
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts._git import git_output  # noqa: E402
from scripts.checks import ensure_utf8_stdout, markdown_page  # noqa: E402

DOCS_DIR = REPO_ROOT / "docs"
# The sequencing record, and an amendment kept in a file of its own beside it.
SEQUENCING_NAME = "v*-sequencing*.md"
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
TEMPLATE_SECTIONS = (
    "Context",
    SECTION_TITLE,
    "What changes",
    "What does not change",
    "Downstream pointers moved in the same PR",
    "Decision / next steps",
)
HEADING_FORM = "`## Amendment YYYY-MM-DD — <title>`"

_AMENDMENT_RE = re.compile(r"^Amendment (\d{4})-(\d{2})-(\d{2})\b")
# A heading names an amendment when "amendment" and a date appear anywhere in
# it, whatever the case, level, word order, markup or dashes between the digits.
_NAMES_AMENDMENT_RE = re.compile(r"\bamendments?\b", re.IGNORECASE)
_ANY_DATE_RE = re.compile(
    r"(?<!\d)(\d{4})[-\u2010-\u2015/.](\d{1,2})[-\u2010-\u2015/.](\d{1,2})(?!\d)"
)
_SETEXT_RE = re.compile(r"^ {0,3}(?:=+|-+)[ \t]*$")
_ITEM_RE = re.compile(r"^\s*(?:[-+*]|\d{1,9}[.)])\s")
# "<fill in>", but not an e-mail or an HTML tag with attributes…
_PLACEHOLDER_RE = re.compile(r"^<[^<>=@]*>$")
# …nor an autolink: a scheme, then no space ("<https://…>", "<mailto:…>").
_AUTOLINK_RE = re.compile(r"^<[a-z][a-z0-9+.-]{1,31}:[^\s<>]*>$", re.IGNORECASE)
# A deferred value: the marker alone, or followed by ":", " -" or " (".
_DEFERRED_RE = re.compile(
    r"^(?:tbd|tba|todo|fixme|xxx|pending)(?:[\s.!?]*$|\s*:|\s+[-–—(])", re.IGNORECASE
)
# Code, emphasis or brackets around a value.
_WRAPPING_RE = re.compile(r"^[\s`*_\[(]+|[\s`*_\])]+$")


class Amendment(NamedTuple):
    line: int
    date: date
    title: str
    body: tuple[str, ...]  # the lines under the heading as the page shows them, to its end


class _Heading(NamedTuple):
    at: int  # the line index
    level: int
    title: str
    atx: bool  # False for a setext heading, which only the amendment scan reads


def sequencing_docs(docs_dir: Path) -> list[Path]:
    """Every tracked ``v*-sequencing*.md`` under *docs_dir*, at any depth.

    Git's file list is the record: an untracked draft is not on main, so CI
    would not read it and the hook must not either. Outside a checkout the
    directory is walked instead, local-only review reports aside.
    """
    listed = git_output(docs_dir.parent, "ls-files", "-z", "--", f"{docs_dir.name}/")
    if listed is None:
        paths = [
            p for p in docs_dir.rglob("*.md") if p.relative_to(docs_dir).parts[0] != LOCAL_ONLY_DIR
        ]
    else:
        paths = [docs_dir.parent / name for name in listed.split("\0") if name]
    return sorted(p for p in paths if fnmatchcase(p.name, SEQUENCING_NAME) and p.is_file())


def _plain(text: str) -> str:
    """*text* without emphasis markers or repeated spaces.

    An underscore inside a word (``MEMORY_BUDGET_TOKENS``) is not emphasis on
    GitHub, so it stays.
    """
    return " ".join(re.sub(r"\*+|(?<!\w)_+|_+(?!\w)", "", text).split())


def _headings(shown: Sequence[str]) -> Iterator[tuple[int, int, str]]:
    """(index, level, text without emphasis) of every ATX heading of a rendered page."""
    for i, line in enumerate(shown):
        found = markdown_page.heading(line)
        if found:
            yield i, found[0], _plain(found[1])


def _page_headings(shown: Sequence[str]) -> list[_Heading]:
    """Every heading of a rendered page: the ATX ones, and a line over ``===`` or ``---``."""
    atx = {i: (level, title) for i, level, title in _headings(shown)}
    found: list[_Heading] = []
    for i, line in enumerate(shown):
        if i in atx:
            level, title = atx[i]
            found.append(_Heading(i, level, title, True))
        elif i + 1 < len(shown) and _SETEXT_RE.match(shown[i + 1]) and _paragraph(line):
            found.append(_Heading(i, 1 if "=" in shown[i + 1] else 2, _plain(line), False))
    return found


def _paragraph(line: str) -> bool:
    """True when *line* can be the text of a setext heading."""
    return bool(
        line.strip()
        and len(line) - len(line.lstrip(" ")) < 4
        and not _SETEXT_RE.match(line)
        and not _ITEM_RE.match(line)
        and markdown_page.cells(line) is None
    )


def _is_section(title: str) -> bool:
    return title.lower().startswith(SECTION_TITLE.lower())


def _in_template(title: str) -> bool:
    """True for a section the amendment template writes at level 2, inside the amendment."""
    return _is_section(title) or title.lower() in (t.lower() for t in TEMPLATE_SECTIONS)


def _real_date(year: str, month: str, day: str) -> date | None:
    """The date these digits name, or None when it does not exist."""
    try:
        return date(int(year), int(month), int(day))
    except ValueError:
        return None


def amendments(text: str) -> list[Amendment]:
    """Every ``## Amendment`` section of a sequencing document with a real date."""
    return _read(markdown_page.rendered(text.splitlines()))[0]


def _read(shown: Sequence[str]) -> tuple[list[Amendment], list[tuple[int, str, str]]]:
    """A rendered page's amendments, and (line, title, problem) for each heading not read.

    A level-2 ``Amendment YYYY-MM-DD`` heading with a real date opens an
    amendment. It runs to the next level-1 or -2 heading, except the sections
    the amendment template writes at level 2 (Context, External evidence, …):
    pasted as they are, they still belong to it.

    Any other heading naming a dated amendment fails when the date does not
    exist or falls on or after the rule: another level, case, word order or
    date form, or a setext heading. The exception is a sub-heading inside an
    amendment at least as recent as the date it names, which refers to that
    amendment rather than opening a new one.
    """
    found: list[Amendment] = []
    problems: list[tuple[int, str, str]] = []
    current: Amendment | None = None  # the open amendment, its body not yet cut

    def close(end: int) -> None:
        if current is not None:
            found.append(current._replace(body=tuple(shown[current.line : end])))

    for head in _page_headings(shown):
        if head.atx and head.level <= 2 and not _in_template(head.title):
            close(head.at)
            readable = _AMENDMENT_RE.match(head.title) if head.level == 2 else None
            when = _real_date(*readable.groups()) if readable else None
            current = Amendment(head.at + 1, when, head.title, ()) if when else None
            if current is not None:
                continue
        named = _ANY_DATE_RE.search(head.title)
        if not (named and _NAMES_AMENDMENT_RE.search(head.title)):
            continue
        named_date = _real_date(*named.groups())
        if named_date is None:
            problem = f"{named.group(0)} is not a date (write YYYY-MM-DD)"
        elif named_date >= RULE_FROM and not (
            head.atx and head.level > 2 and current is not None and current.date >= named_date
        ):
            problem = f"not in the {HEADING_FORM} form, so its evidence goes unread"
        else:
            continue
        problems.append((head.at + 1, head.title, problem))
    close(len(shown))
    return found, problems


def evidence_rows(lines: Sequence[str]) -> dict[str, list[str]] | None:
    """The External evidence table as {lower-cased label: [value, …]}, or None with no section.

    The section runs from its heading to the next heading of the same or a
    higher level. Only a rendered table counts — a header row with a
    ``|---|`` divider under it, then its body rows — so a table inside a code
    fence or an HTML comment fills nothing.
    """
    return _evidence_rows(markdown_page.rendered(lines))


def _evidence_rows(shown: Sequence[str]) -> dict[str, list[str]] | None:
    """:func:`evidence_rows` of lines already rendered."""
    heads = list(_headings(shown))
    for n, (i, level, title) in enumerate(heads):
        if not _is_section(title):
            continue
        end = next((j for j, lv, _ in heads[n + 1 :] if lv <= level), len(shown))
        return _table_rows(shown[i + 1 : end])
    return None


def _table_rows(lines: Sequence[str]) -> dict[str, list[str]]:
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
    """A value that records nothing: no letter or digit, a placeholder, or TBD.

    A placeholder or TBD still counts when wrapped in code, emphasis or brackets.
    """
    text = value.strip()
    core = _WRAPPING_RE.sub("", text)
    return (
        not re.search(r"[^\W_]", text)
        or bool(_PLACEHOLDER_RE.match(core) and not _AUTOLINK_RE.match(core))
        or bool(_DEFERRED_RE.match(core))
    )


def evidence_problems(amendment: Amendment) -> list[str]:
    """What the amendment's External evidence section lacks; empty when complete."""
    rows = _evidence_rows(amendment.body)
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
        lines = doc.read_text(encoding="utf-8").splitlines()
        opened = markdown_page.unclosed(lines)
        if opened is not None:
            failures.append(
                f"  {rel}:{opened + 1}: a code fence or HTML comment opened here never closes,"
                " so nothing after it is read — close it"
            )
        found, problems = _read(markdown_page.rendered(lines))
        failures.extend(f"  {rel}:{n}: {title} — {p}" for n, title, p in problems)
        for a in found:
            if a.date < RULE_FROM:
                continue
            judged += 1
            failures.extend(f"  {rel}:{a.line}: {a.title} — {p}" for p in evidence_problems(a))
    since = RULE_FROM.isoformat()
    if judged:
        print(
            f"[SCAN] Checked {judged} amendment(s) dated {since} or later"
            f" in {len(docs)} sequencing doc(s)"
        )
    else:
        print(
            f"[FAIL] Found no amendment dated {since} or later in a tracked"
            f" {DOCS_DIR.name}/**/{SEQUENCING_NAME}, so this check read nothing — if the"
            " sequencing record moved, point scripts/checks/amendment_evidence.py at it."
        )
    if failures:
        print(
            f"\n[FAIL] {len(failures)} gap(s) in External evidence sections"
            " (docs/methodology/decisions.md rule 6; fill each row, 'none' if nothing happened):"
        )
        print("\n".join(failures))
    if failures or not judged:
        return 1
    print(f"[OK] Every amendment since {since} records its external evidence.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
