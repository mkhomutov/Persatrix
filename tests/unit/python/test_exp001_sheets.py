"""EXP-001 harness — the two people's score sheets (the scoring PR).

Each person scores their packets alone, in their own order, and writes the
scores in a sheet of their own: a CSV file beside their packets, one row per
packet, which the harness writes blank. A memo's row takes C1 to C5, each
0, 1 or 2, but C2 on a memo whose key lists no earlier facts; a recall
reply's row marks each of its key's questions right or wrong. A cell that
does not apply holds a dash. A row not yet filled is a packet left; anything
that is no score at all, or a packet the person was not given, is refused,
never guessed at.
"""

from __future__ import annotations

import csv
import itertools
from pathlib import Path

import pytest

from evaluators.exp001.judge import Packet
from evaluators.exp001.packets import blind
from evaluators.exp001.rating import Drawn
from evaluators.exp001.scoring import MemoRef
from evaluators.exp001.sheets import (
    SheetError,
    columns,
    read_sheet,
    sheet_path,
    write_blank_sheets,
)

from ._exp001_judge_test_helpers import CONTROL, PLAN, PRACTICE, RECALL


def _drawn() -> Drawn:
    """The practice series' plan memo (p1), control memo (p2) and recall reply
    (p3); person-1 reads them p3, p1, p2 and person-2 p1, p2, p3."""
    counter = itertools.count(1)
    blinded = blind(
        [PRACTICE],
        {MemoRef("C", PRACTICE.id, PLAN.id): "## Recommendation\nB.",
         MemoRef("D", PRACTICE.id, CONTROL.id): "## Recommendation\nC."},
        {MemoRef("C", PRACTICE.id, RECALL.id): "1. May.\n2. $25.\n3. Two."},
        (),
        new_id=lambda: f"p{next(counter)}",
    )
    every: list[Packet] = [*blinded.memo_packets, *blinded.recall_packets]
    packets = {p.id: p for p in every}
    return Drawn(packets, {"person-1": ["p3", "p1", "p2"], "person-2": ["p1", "p2", "p3"],
                           "judge": ["p2", "p3", "p1"]})


def _rows(path: Path) -> list[list[str]]:
    with path.open(newline="") as sheet:
        return list(csv.reader(sheet))


def _fill(path: Path, cells: dict[str, dict[str, str]]) -> None:
    """Fill the blank sheet at *path*: for each packet, the cells named."""
    rows = _rows(path)
    header = rows[0]
    for row in rows[1:]:
        for column, value in cells.get(row[1], {}).items():
            row[header.index(column)] = value
    with path.open("w", newline="") as sheet:
        csv.writer(sheet).writerows(rows)


_FULL = {
    "p1": {"C1": "2", "C2": "1", "C3": "2", "C4": "0", "C5": "2"},
    "p2": {"C1": "1", "C3": "1", "C4": "1", "C5": "2"},
    "p3": {"R1": "right", "R2": "wrong", "R3": "right"},
}


class TestBlankSheets:
    def test_each_person_gets_a_sheet_in_their_own_order(self, tmp_path: Path) -> None:
        assert write_blank_sheets(tmp_path, _drawn()) == ("person-1", "person-2")
        assert [row[1] for row in _rows(sheet_path(tmp_path, "person-1"))[1:]] == ["p3", "p1", "p2"]
        assert [row[1] for row in _rows(sheet_path(tmp_path, "person-2"))[1:]] == ["p1", "p2", "p3"]
        assert sheet_path(tmp_path, "person-1") == tmp_path / "raters" / "person-1-scores.csv"

    def test_a_cell_that_does_not_apply_holds_a_dash(self, tmp_path: Path) -> None:
        write_blank_sheets(tmp_path, _drawn())
        header, *rows = _rows(sheet_path(tmp_path, "person-2"))
        assert header == columns(_drawn()) == [
            "number", "packet", "kind", "C1", "C2", "C3", "C4", "C5", "R1", "R2", "R3",
        ]
        assert rows == [
            ["1", "p1", "memo", "", "", "", "", "", "-", "-", "-"],
            ["2", "p2", "memo", "", "-", "", "", "", "-", "-", "-"],  # no earlier facts: no C2
            ["3", "p3", "recall", "-", "-", "-", "-", "-", "", "", ""],
        ]

    def test_a_sheet_already_there_is_never_written_again(self, tmp_path: Path) -> None:
        write_blank_sheets(tmp_path, _drawn())
        _fill(sheet_path(tmp_path, "person-1"), _FULL)
        assert write_blank_sheets(tmp_path, _drawn()) == ()
        assert read_sheet(sheet_path(tmp_path, "person-1"), _drawn(), "person-1").left == ()


class TestReadingASheet:
    @pytest.fixture
    def sheet(self, tmp_path: Path) -> Path:
        write_blank_sheets(tmp_path, _drawn())
        return sheet_path(tmp_path, "person-1")

    def test_a_filled_sheet_gives_every_score(self, sheet: Path) -> None:
        _fill(sheet, _FULL)
        read = read_sheet(sheet, _drawn(), "person-1")
        assert read.left == ()
        assert read.memos == {
            "p1": {"C1": 2, "C2": 1, "C3": 2, "C4": 0, "C5": 2},
            "p2": {"C1": 1, "C2": None, "C3": 1, "C4": 1, "C5": 2},
        }
        assert read.recall == {"p3": {"R1": True, "R2": False, "R3": True}}

    def test_a_packet_partly_scored_is_left_in_the_persons_order(self, sheet: Path) -> None:
        _fill(sheet, {"p1": {**_FULL["p1"], "C5": ""}, "p2": _FULL["p2"]})
        read = read_sheet(sheet, _drawn(), "person-1")
        assert read.left == ("p3", "p1")
        assert set(read.memos) == {"p2"}

    def test_marks_and_spaces_are_read_however_they_are_cased(self, sheet: Path) -> None:
        _fill(sheet, {**_FULL, "p1": {**_FULL["p1"], "C1": " 2 "},
                      "p3": {"R1": "Right", "R2": "WRONG ", "R3": "right"}})
        read = read_sheet(sheet, _drawn(), "person-1")
        assert read.recall["p3"] == {"R1": True, "R2": False, "R3": True}
        assert read.memos["p1"]["C1"] == 2

    @pytest.mark.parametrize(("cells", "match"), [
        ({"p1": {"C1": "3"}}, r"p1: C1 is '3', not 0, 1 or 2"),
        ({"p1": {"C1": "2.0"}}, r"p1: C1 is '2.0', not 0, 1 or 2"),
        ({"p3": {"R2": "yes"}}, r"p3: R2 is 'yes', not right or wrong"),
        ({"p2": {"C2": "1"}}, r"p2: C2 does not apply"),
        ({"p1": {"R1": "right"}}, r"p1: R1 does not apply"),
    ])
    def test_a_cell_that_is_no_score_is_refused(
        self, sheet: Path, cells: dict[str, dict[str, str]], match: str,
    ) -> None:
        _fill(sheet, cells)
        with pytest.raises(SheetError, match=match):
            read_sheet(sheet, _drawn(), "person-1")

    def test_a_packet_the_person_was_not_given_is_refused(self, sheet: Path) -> None:
        rows = _rows(sheet)
        rows[1][1] = "1.2E+46"  # what a spreadsheet can make of a packet ID
        with sheet.open("w", newline="") as out:
            csv.writer(out).writerows(rows)
        with pytest.raises(SheetError, match=r"1\.2E\+46.*not one of person-1's packets"):
            read_sheet(sheet, _drawn(), "person-1")

    def test_a_packet_listed_twice_or_left_out_is_refused(self, sheet: Path) -> None:
        rows = _rows(sheet)
        with sheet.open("w", newline="") as out:
            csv.writer(out).writerows([*rows, rows[1]])
        with pytest.raises(SheetError, match="p3 is listed twice"):
            read_sheet(sheet, _drawn(), "person-1")
        with sheet.open("w", newline="") as out:
            csv.writer(out).writerows(rows[:-1])
        with pytest.raises(SheetError, match="no row for p2"):
            read_sheet(sheet, _drawn(), "person-1")

    def test_columns_changed_are_refused(self, sheet: Path) -> None:
        rows = _rows(sheet)
        rows[0][3] = "Criterion 1"
        with sheet.open("w", newline="") as out:
            csv.writer(out).writerows(rows)
        with pytest.raises(SheetError, match="columns"):
            read_sheet(sheet, _drawn(), "person-1")

    def test_a_spreadsheets_byte_order_mark_and_blank_rows_do_no_harm(self, sheet: Path) -> None:
        _fill(sheet, _FULL)
        sheet.write_text("\ufeff" + sheet.read_text() + ",,,,,,,,,,\n\n")
        assert read_sheet(sheet, _drawn(), "person-1").left == ()

    def test_a_sheet_that_is_missing_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(SheetError, match="no score sheet"):
            read_sheet(sheet_path(tmp_path, "person-1"), _drawn(), "person-1")
