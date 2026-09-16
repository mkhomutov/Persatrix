"""What a Markdown page shows once rendered: its lines, headings and table cells.

Shared by the doc checks that read Markdown structure: doc links (heading
anchors), plan status and ROADMAP status (table rows). Each used to parse
Markdown its own way, and they disagreed with the page GitHub renders and with
each other: one closed a code fence on any line starting with three
backticks, so a ```` block that shows a ``` line hid the headings after it.

This is not a full Markdown parser. It reads line by line and follows the
CommonMark rules these checks need:

- A code fence opens on a line of three or more backticks or tildes and closes
  only on a bare line of the same character at least as long. A backtick
  fence's info string cannot hold a backtick (```` ```py``` ```` is inline
  code), and a fence nobody closes runs to the end of the page.
- An HTML comment opens on a line starting with ``<!--`` and hides every line
  up to and including the one holding ``-->``.
- A heading is an ATX heading (``## Title``); a closing run of ``#`` is not
  part of its text. Setext underlines (``===`` / ``---``) are not read: no
  check needs them, and they would misfire on front-matter and thematic breaks.
- A table row starts with ``|``; the closing ``|`` is optional, as GitHub
  renders it.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence

_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$")
_DIVIDER_CELL_RE = re.compile(r"^:?-+:?$")


def rendered(lines: Sequence[str]) -> list[str]:
    """*lines* as the page shows them: a line inside a code fence or an HTML comment is blank.

    Blanking rather than dropping keeps each index on the same line, so a
    check can still report line numbers, and a hidden block still ends a table
    the way it does on the page.
    """
    shown: list[str] = []
    fence = ""
    comment = False
    for line in lines:
        m = _FENCE_RE.match(line)
        if fence:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence):
                fence = "" if not m.group(2).strip() else fence
            shown.append("")
        elif comment:
            comment = "-->" not in line
            shown.append("")
        elif m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
            fence = m.group(1)
            shown.append("")
        elif line.lstrip().startswith("<!--"):
            comment = "-->" not in line
            shown.append("")
        else:
            shown.append(line)
    return shown


def heading(line: str) -> tuple[int, str] | None:
    """The (level, text) of an ATX heading line, or None when *line* is not one."""
    m = _HEADING_RE.match(line)
    return (len(m.group(1)), m.group(2)) if m else None


def headings(lines: Sequence[str]) -> Iterator[tuple[int, int, str]]:
    """(index, level, text) of every heading the page shows."""
    for i, line in enumerate(rendered(lines)):
        found = heading(line)
        if found:
            yield i, found[0], found[1]


def cells(line: str) -> list[str] | None:
    """A table row's cells, stripped, or None when *line* is not a row."""
    inner = line.strip()
    if not inner.startswith("|") or len(inner) < 2:
        return None
    inner = inner[1:-1] if inner.endswith("|") else inner[1:]
    return [c.strip() for c in inner.split("|")]


def is_divider(row: Sequence[str]) -> bool:
    """True for the ``|---|:--:|`` line under a table's header row."""
    return all(_DIVIDER_CELL_RE.match(c) for c in row)
