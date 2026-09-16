"""What a Markdown page shows once rendered: its lines, headings and table cells.

Shared by the doc checks that read Markdown structure: doc links (heading
anchors), plan status and ROADMAP status (table rows), and amendment evidence
(headings and table rows). Each used to parse
Markdown its own way, and they disagreed with the page GitHub renders and with
each other: one closed a code fence on any line starting with three
backticks, so a ```` block that shows a ``` line hid the headings after it.

This is not a full Markdown parser. It reads line by line and follows the
CommonMark rules these checks need:

- A code fence opens on a line of three or more backticks or tildes and closes
  only on a bare line of the same character at least as long. A backtick
  fence's info string cannot hold a backtick (```` ```py``` ```` is inline
  code). Neither line may sit four or more spaces past the start of the list
  item it is in, or of the page outside a list: that indent makes it code. A
  fence in a list item ends with the item. Any other fence nobody closes runs
  to the end of the page, and :func:`unclosed` names its first line, so a
  check can fail instead of passing on a page it did not read.
- An HTML comment opens on a line starting with ``<!--`` and hides every line
  up to and including the one holding ``-->``.
- A heading is an ATX heading (``## Title``, up to three spaces in); a
  closing run of ``#`` is not part of its text. Setext underlines (``===`` /
  ``---``) are not read: no check needs them, and they would misfire on
  front-matter and thematic breaks.
- A table row starts or ends with ``|``. GitHub needs neither outer pipe
  inside a table, but a line with neither is not read: telling it from prose
  takes the table around it, which this does not track.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence

_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")
_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$")
_DIVIDER_CELL_RE = re.compile(r"^:?-+:?$")
# A list item's first line; its content starts after the marker and the spaces after it.
_ITEM_RE = re.compile(r"^(\s*(?:[-+*]|\d{1,9}[.)])\s+)\S")


def rendered(lines: Sequence[str]) -> list[str]:
    """*lines* as the page shows them: a line inside a code fence or an HTML comment is blank.

    Blanking rather than dropping keeps each index on the same line, so a
    check can still report line numbers, and a hidden block still ends a table
    the way it does on the page.
    """
    return [shown for shown, _ in _scan(lines)]


def unclosed(lines: Sequence[str]) -> int | None:
    """The index of the line opening a code fence or HTML comment the page never closes.

    Every line after it is hidden, so a check reading rows or headings would
    pass on a page it has not read. None when every block closes.
    """
    still_open = None
    for _, still_open in _scan(lines):
        pass
    return still_open


def _scan(lines: Sequence[str]) -> Iterator[tuple[str, int | None]]:
    """Each line as shown (blank when hidden), and where the block still open after it starts."""
    fence = ""  # the open fence's run of backticks or tildes
    comment = False
    start = 0  # the open fence's or comment's first line
    item = 0  # the column the content of the current list item starts at; 0 outside a list
    container = 0  # the same column for the list item the open fence is in
    for i, line in enumerate(lines):
        wide = line.expandtabs(4)
        indent = len(wide) - len(wide.lstrip())
        if fence and wide.strip() and indent < container:
            fence = ""  # the list item ended, and its fence with it
        m = _FENCE_RE.match(line)
        shown = ""
        if fence:
            if (
                m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence)
                and not m.group(2).strip() and indent - container < 4
            ):
                fence = ""
        elif comment:
            comment = "-->" not in line
        else:
            if marker := _ITEM_RE.match(wide):
                item = len(marker.group(1))
            elif wide.strip() and indent < item:
                item = indent
            if m and indent - item < 4 and not (m.group(1)[0] == "`" and "`" in m.group(2)):
                fence, container, start = m.group(1), item, i
            elif wide.lstrip().startswith("<!--"):
                comment, start = "-->" not in line, i
            else:
                shown = line
        yield shown, (start if fence or comment else None)


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
    if len(inner) < 2 or not (inner.startswith("|") or inner.endswith("|")):
        return None
    return [c.strip() for c in inner.removeprefix("|").removesuffix("|").split("|")]


def is_divider(row: Sequence[str]) -> bool:
    """True for the ``|---|:--:|`` line under a table's header row."""
    return bool(row) and all(_DIVIDER_CELL_RE.match(c) for c in row)
