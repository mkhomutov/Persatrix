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
person was not given, one listed twice or one left out, so a sheet a
spreadsheet changed is never read as other scores than its person gave.
"""

from __future__ import annotations

import csv
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
            kind = "memo" if isinstance(packet, MemoPacket) else "recall"
            rows.append([str(number), packet.id, kind,
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
    the person was not given, a packet listed twice or one left out is refused."""
    try:
        # A spreadsheet may begin the file with a byte order mark.
        with path.open(newline="", encoding="utf-8-sig") as sheet:
            rows = list(csv.reader(sheet))
    except FileNotFoundError:
        raise SheetError(f"{path}: no score sheet for {person}") from None
    if not rows or _trimmed(rows[0]) != header:
        raise SheetError(f"{path}: the columns are not {', '.join(header)}")
    mine = {p.id for p in order}
    cells: dict[str, dict[str, str]] = {}
    for number, row in enumerate(rows[1:], start=2):
        if not any(cell.strip() for cell in row):
            continue  # a blank row a spreadsheet left
        packet = row[1].strip() if len(row) > 1 else ""
        if packet not in mine:
            raise SheetError(
                f"{path}:{number}: packet {packet!r} is not one of {person}'s packets; a "
                "spreadsheet can change an ID it reads as a number, so open the sheet with "
                "the packet column as text",
            )
        if packet in cells:
            raise SheetError(f"{path}:{number}: {packet} is listed twice")
        cells[packet] = {c: (row[i].strip() if i < len(row) else "")
                         for i, c in enumerate(header) if i >= len(_FIXED)}
    absent = [p.id for p in order if p.id not in cells]
    if absent:
        raise SheetError(f"{path}: no row for {', '.join(absent)}")
    return cells


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
