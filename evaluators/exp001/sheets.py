"""The two people's score sheets, which the harness writes blank and reads back.

Part 2 §2 of the pre-registration: two people score every packet, each
alone, each in their own order (:mod:`evaluators.exp001.rating`). Each
writes their scores in a sheet of their own, ``raters/<person>-scores.csv``,
beside the packets they read. It is a CSV file, so a spreadsheet opens it,
with one row per packet in the person's order:

- ``number`` and ``packet``: where the packet comes in the person's own
  file, and its ID, as the packet's heading gives them;
- ``kind``: ``memo`` or ``recall``;
- ``C1`` to ``C5``: a memo's five scores, each 0, 1 or 2. C2 is not scored
  on a memo whose key lists no earlier facts, a control plan (part 2 §3);
- ``R1`` and on: a recall reply's mark on each of its key's questions,
  ``right`` or ``wrong``.

A cell that does not apply to the packet holds a dash. The harness reads a
sheet at any time: a packet with a cell still empty is not yet scored, and
is left. Anything else that is not a score is refused, as is a packet the
person was not given, one listed twice or one left out. A row's ``number``
and ``kind`` must still be its packet's, so an ID that moved to another
row, as sorting one column apart from the rest moves it, is refused too.
That is how the harness keeps from reading a sheet a spreadsheet changed as
other scores than its person gave.

A spreadsheet saves the sheet in its own ways, and those that keep every
cell are read: a byte order mark, any line ends, semicolons or tabs between
the cells, and blank rows or columns at the end. The file must be UTF-8.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from evaluators.exp001.judge import Packet, packet_kind
from evaluators.exp001.materials import MeetingKind
from evaluators.exp001.packets import MemoPacket, RecallPacket
from evaluators.exp001.rating import PEOPLE, RATER_FILES, Drawn
from evaluators.exp001.scoring import CRITERIA, MEMORY_CRITERION

NOT_SCORED = "-"
_FIXED = ("number", "packet", "kind")
_SCALE = {"0": 0, "1": 1, "2": 2}
_MARKS = {"right": True, "wrong": False}
# What a spreadsheet may put between the cells: the comma the harness writes,
# a semicolon where the comma is the decimal mark, or a tab.
_DELIMITERS = (",", ";", "\t")
_WHOLE = re.compile(r"(\d+)(?:\.0+)?")  # a row's number, as a spreadsheet may respell it


class SheetError(ValueError):
    """A score sheet holds something that is not a score, or not its person's packets."""


@dataclass(frozen=True)
class Sheet:
    """What a person's sheet holds so far."""

    memos: dict[str, dict[str, int | None]]  # each memo scored, by packet ID
    recall: dict[str, dict[str, bool]]  # each recall reply marked, by packet ID
    left: tuple[str, ...]  # packets not yet scored, in the person's order


def sheet_path(directory: Path, person: str) -> Path:
    """Where *person* writes their scores, beside their packets in *directory*."""
    return directory / RATER_FILES / f"{person}-scores.csv"


def columns(drawn: Drawn) -> list[str]:
    """The sheet's columns: the recall keys' questions in order, after the criteria."""
    questions = {q for p in drawn.packets.values() if isinstance(p, RecallPacket) for q in p.key}
    return [*_FIXED, *CRITERIA, *sorted(questions, key=_question_order)]


def write_blank_sheets(directory: Path, drawn: Drawn) -> tuple[str, ...]:
    """Write each person's sheet blank, unless it is there already; the people written for."""
    header = columns(drawn)
    written = []
    for person in PEOPLE:
        path = sheet_path(directory, person)
        if path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [header]
        for number, packet in enumerate(drawn.order(person), start=1):
            applies = _applies(packet)
            rows.append([str(number), packet.id, _kind(packet),
                         *("" if c in applies else NOT_SCORED for c in header[len(_FIXED):])])
        with path.open("x", newline="") as sheet:
            csv.writer(sheet).writerows(rows)
        written.append(person)
    return tuple(written)


def read_sheet(path: Path, drawn: Drawn, person: str) -> Sheet:
    """The scores *person*'s sheet at *path* holds so far, for their packets in *drawn*."""
    order = drawn.order(person)
    cells = _cells(path, columns(drawn), order, person)
    memos: dict[str, dict[str, int | None]] = {}
    recall: dict[str, dict[str, bool]] = {}
    left = []
    for packet in order:
        given = _given(path, packet, cells[packet.id])
        if len(given) < len(_applies(packet)):
            left.append(packet.id)
        elif isinstance(packet, MemoPacket):
            memos[packet.id] = {c: _score(path, packet.id, c, given[c]) if c in given else None
                                for c in CRITERIA}
        else:
            recall[packet.id] = {q: _mark(path, packet.id, q, given[q]) for q in packet.key}
    return Sheet(memos, recall, tuple(left))


def _cells(
    path: Path, header: list[str], order: Sequence[Packet], person: str,
) -> dict[str, dict[str, str]]:
    """Each packet's row, as its score columns' cells; a row for a packet
    the person was not given, a packet listed twice or one left out is
    refused, and so is a row whose number or kind is not its packet's."""
    rows = _rows(path, header, person)
    places = {p.id: place for place, p in enumerate(order, start=1)}
    cells: dict[str, dict[str, str]] = {}
    for number, row in enumerate(rows[1:], start=2):
        if not any(cell.strip() for cell in row):
            continue  # a blank row a spreadsheet left
        given, packet, kind = (row[i].strip() if i < len(row) else "" for i in range(len(_FIXED)))
        if packet not in places:
            raise SheetError(
                f"{path}:{number}: packet {packet!r} is not one of {person}'s packets; a "
                "spreadsheet can change an ID it reads as a number. "
                f"{_put_back(given, order, person)}, and keep the packet column as text "
                "whenever a spreadsheet opens the sheet",
            )
        if packet in cells:
            raise SheetError(f"{path}:{number}: {packet} is listed twice")
        place, is_a = places[packet], _kind(order[places[packet] - 1])
        if (given and _whole(given) != place) or (kind and kind.lower() != is_a):
            raise SheetError(
                f"{path}:{number}: {packet} is number {place} of {person}'s packets and a "
                f"{is_a}, but its row gives number {given!r} and kind {kind!r}: an ID has "
                "moved from its row, as sorting the packet column apart from the others does",
            )
        cells[packet] = {c: (row[i].strip() if i < len(row) else "")
                         for i, c in enumerate(header) if i >= len(_FIXED)}
    absent = [p.id for p in order if p.id not in cells]
    if absent:
        raise SheetError(f"{path}: no row for {', '.join(absent)}")
    return cells


def _rows(path: Path, header: list[str], person: str) -> list[list[str]]:
    """The sheet's rows, whichever of the ways a spreadsheet saves it."""
    try:
        # A spreadsheet may begin the file with a byte order mark.
        text = path.read_bytes().decode("utf-8-sig")
    except FileNotFoundError:
        raise SheetError(f"{path}: no score sheet for {person}") from None
    except UnicodeDecodeError:
        raise SheetError(
            f'{path}: the sheet is not saved as UTF-8; save it again as CSV in UTF-8 ("CSV UTF-8 '
            '(Comma delimited)" in Excel)',
        ) from None
    for delimiter in _DELIMITERS:
        try:
            rows = list(csv.reader(io.StringIO(text, newline=""), delimiter=delimiter))
        except csv.Error as exc:
            raise SheetError(f"{path}: not a sheet a spreadsheet saves as CSV ({exc})") from None
        if rows and _trimmed(rows[0]) == header:
            return rows
    raise SheetError(f"{path}: the columns are not {', '.join(header)}")


def _put_back(given: str, order: Sequence[Packet], person: str) -> str:
    """Which ID a row that lost its own should hold, by the row's number;
    without a number that is one of the person's, where to find it."""
    place = _whole(given)
    if place is None or not 1 <= place <= len(order):
        return f"Put back the ID from that packet's heading in {person}.txt"
    return f"Number {place} of {person}'s packets is {order[place - 1].id}: put that ID back"


def _whole(cell: str) -> int | None:
    """A row's number as a whole number, None when the cell holds no such thing."""
    found = _WHOLE.fullmatch(cell)
    return int(found.group(1)) if found else None


def _kind(packet: Packet) -> str:
    return "memo" if isinstance(packet, MemoPacket) else "recall"


def _given(path: Path, packet: Packet, cells: Mapping[str, str]) -> dict[str, str]:
    """The cells of *packet*'s row that hold a score, each checked to be
    one; a cell that does not apply must hold a dash or nothing."""
    applies = _applies(packet)
    given = {}
    for column, cell in cells.items():
        if column not in applies:
            if cell not in ("", NOT_SCORED):
                raise SheetError(f"{path}: {packet.id}: {column} does not apply to this packet")
        elif cell:
            check = _score if isinstance(packet, MemoPacket) else _mark
            check(path, packet.id, column, cell)
            given[column] = cell
    return given


def _applies(packet: Packet) -> tuple[str, ...]:
    """The columns a packet is scored in."""
    if isinstance(packet, RecallPacket):
        return tuple(packet.key)
    if packet_kind(packet) is MeetingKind.CONTROL:
        return tuple(c for c in CRITERIA if c != MEMORY_CRITERION)
    return CRITERIA


def _score(path: Path, packet: str, criterion: str, cell: str) -> int:
    if cell not in _SCALE:
        raise SheetError(f"{path}: {packet}: {criterion} is {cell!r}, not 0, 1 or 2")
    return _SCALE[cell]


def _mark(path: Path, packet: str, question: str, cell: str) -> bool:
    mark = _MARKS.get(cell.lower())
    if mark is None:
        raise SheetError(f"{path}: {packet}: {question} is {cell!r}, not right or wrong")
    return mark


def _trimmed(header: Sequence[str]) -> list[str]:
    """A header row's names, without the empty ones a spreadsheet may add at its end."""
    names = [name.strip() for name in header]
    while names and not names[-1]:
        names.pop()
    return names


def _question_order(question: str) -> tuple[int, str]:
    number = re.fullmatch(r"R(\d+)", question)
    return (int(number.group(1)), "") if number else (0, question)
