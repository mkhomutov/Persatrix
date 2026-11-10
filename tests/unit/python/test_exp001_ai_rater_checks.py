"""EXP-001 harness — the checks on the AI raters that come before the seal opens.

A rater's session audits its own folder of run records: every run it
started is logged, and every logged record is still there, unchanged
(docs/experiments/EXP-001-deviation-ai-raters.md, rule 6). Both raters'
scored prompts must hold one set of notes, checked before the first scored
run and again at the seal (rules 7 and 8). Then, once every score is in,
each sheet must hold exactly what its entered runs gave, and anything that
differs between the runs' set-ups is listed for the result (rule 8). In
practice, the cells the two raters scored differently are listed.

The records here have the shape Claude Code 2.1 keeps a run's record in
(``_exp001_ai_rater_test_helpers``).
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from evaluators.exp001 import __main__ as command
from evaluators.exp001.ai_rater_checks import differences, ready, same_notes, unentered
from evaluators.exp001.ai_rater_runs import MODEL, brief
from evaluators.exp001.ai_raters import LINE_LIMIT, PROMPT_LIMIT, RaterRefused, keep
from evaluators.exp001.packets import RecallPacket
from evaluators.exp001.rating import PEOPLE, read_packets
from evaluators.exp001.sheets import sheet_path

from ._exp001_ai_rater_test_helpers import (
    answer as _answer,
)
from ._exp001_ai_rater_test_helpers import (
    as_window,
    held_practice,
    notes_file,
    record,
    said,
)
from ._exp001_ai_rater_test_helpers import (
    batches as _batches,
)
from ._exp001_ai_rater_test_helpers import (
    enter_answer as _enter,
)
from ._exp001_ai_rater_test_helpers import (
    given as _given,
)
from ._exp001_ai_rater_test_helpers import (
    out_of as _out,
)


@pytest.fixture(scope="module")
def _held(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One practice run with its blank sheets, built once for the module."""
    return held_practice(tmp_path_factory)


@pytest.fixture
def window(_held: Path, tmp_path: Path) -> Path:
    """The practice run laid out as a scored run's first window, scored with notes."""
    return as_window(_held, tmp_path)


@pytest.fixture
def notes(tmp_path: Path) -> Path:
    return notes_file(tmp_path)


@pytest.fixture
def run(_held: Path, tmp_path: Path) -> Path:
    """The practice run, scored with no notes."""
    return Path(shutil.copytree(_held, tmp_path / "run"))


def _score_every_cell(directory: Path, notes: Path | None, **kept: Any) -> list[Path]:
    """Both raters' batches written and every run entered; each rater's folder."""
    for person in PEOPLE:
        for prompt in _batches(directory, notes, person):
            _enter(directory, person, prompt, _answer(prompt, **kept))
    return [_out(directory, person) for person in PEOPLE]


class TestCheckingEveryRun:
    def test_a_run_never_entered_kept_or_refused_is_listed(
        self, window: Path, notes: Path,
    ) -> None:
        first, second = _batches(window, notes)
        entered, forgotten = _answer(first), _answer(first)
        _enter(window, "person-1", first, entered)
        records = entered.parent
        (listed,) = unentered([first.parent], records)
        assert str(forgotten) in listed and "batch-01" in listed
        with pytest.raises(RaterRefused):
            _enter(window, "person-1", first, forgotten)
        assert unentered([first.parent], records) == []
        assert second.exists()

    def test_a_run_briefed_in_other_words_is_listed_too(self, window: Path, notes: Path) -> None:
        prompt = _batches(window, notes)[0]
        _enter(window, "person-1", prompt, _answer(prompt))
        # As a fork's record begins: the session's own context came with it.
        forked = _answer(prompt, first=f"<fork-boilerplate>One directive.</fork-boilerplate>"
                                       f"\n\nYour directive: {brief(prompt)}")
        (listed,) = unentered([prompt.parent], forked.parent)
        assert str(forked) in listed and "no brief" in listed

    def test_a_folder_that_holds_no_records_is_refused(
        self, window: Path, notes: Path, tmp_path: Path,
    ) -> None:
        prompt = _batches(window, notes)[0]
        (tmp_path / "empty").mkdir()
        for records in (tmp_path / "empty", tmp_path / "nowhere"):
            with pytest.raises(RaterRefused, match="no run records"):
                unentered([prompt.parent], records)

    def test_a_logged_record_that_changed_or_is_gone_is_listed(
        self, window: Path, notes: Path,
    ) -> None:
        prompt = _batches(window, notes)[0]
        entered, again = _answer(prompt), _answer(prompt)
        _enter(window, "person-1", prompt, entered)
        with pytest.raises(RaterRefused):
            _enter(window, "person-1", prompt, again)
        with entered.open("a") as more:
            more.write(json.dumps(said("And one more thing.")) + "\n")
        (listed,) = unentered([prompt.parent], entered.parent)
        assert str(entered) in listed and "changed" in listed
        entered.unlink()
        (listed,) = unentered([prompt.parent], entered.parent)
        assert str(entered) in listed and "is not in" in listed

    def test_a_record_that_cannot_be_read_is_listed(self, window: Path, notes: Path) -> None:
        prompt = _batches(window, notes)[0]
        entered = _answer(prompt)
        _enter(window, "person-1", prompt, entered)
        broken = entered.with_name("agent-broken.jsonl")
        broken.write_text('{"type": "user"\n')
        (listed,) = unentered([prompt.parent], entered.parent)
        assert str(broken) in listed and "cannot be read" in listed

    def test_one_sessions_folders_of_prompts_are_audited_together(
        self, window: Path, notes: Path,
    ) -> None:
        prompt = _batches(window, notes)[0]
        _enter(window, "person-1", prompt, _answer(prompt))
        asking = window.parent / "notes" / "notes-propose.md"
        asking.parent.mkdir()
        asking.write_text("Propose notes on how the anchors apply.\n")
        kept = record(asking, f"model: {MODEL}\n\n- N1: a heading counts.\n")
        keep(asking, kept)
        assert unentered([prompt.parent, asking.parent], kept.parent) == []
        (listed,) = unentered([prompt.parent], kept.parent)
        assert str(kept) in listed and "never entered" in listed


class TestTheNotesAreTheSame:
    def test_both_raters_batches_hold_one_set_of_notes(self, window: Path, notes: Path) -> None:
        for person in PEOPLE:
            _batches(window, notes, person)
        held = hashlib.sha256(notes.read_text().strip().encode()).hexdigest()
        assert same_notes([_out(window, p) for p in PEOPLE]) == held

    def test_different_notes_are_refused(self, window: Path, notes: Path, tmp_path: Path) -> None:
        other = tmp_path / "other.md"
        other.write_text("N1: A heading never counts.\n")
        _batches(window, notes, "person-1")
        _batches(window, other, "person-2")
        with pytest.raises(RaterRefused, match="one set of notes"):
            same_notes([_out(window, p) for p in PEOPLE])

    def test_the_same_words_with_another_ending_are_one_set_of_notes(
        self, window: Path, notes: Path, tmp_path: Path,
    ) -> None:
        other = tmp_path / "other.md"
        other.write_text(notes.read_text() + "\n\n")
        _batches(window, notes, "person-1")
        _batches(window, other, "person-2")
        assert same_notes([_out(window, p) for p in PEOPLE])

    def test_one_folder_named_twice_or_a_folder_with_no_batches_is_refused(
        self, window: Path, notes: Path, tmp_path: Path,
    ) -> None:
        for person in PEOPLE:
            _batches(window, notes, person)
        one = _out(window, "person-1")
        with pytest.raises(RaterRefused, match="each rater"):
            same_notes([one, one])
        (tmp_path / "empty").mkdir()
        with pytest.raises(RaterRefused, match="no batches"):
            same_notes([one, tmp_path / "empty"])

    def test_a_batch_that_cannot_be_read_or_a_changed_prompt_is_refused(
        self, window: Path, notes: Path,
    ) -> None:
        for person in PEOPLE:
            prompt = _batches(window, notes, person)[0]
        outs = [_out(window, p) for p in PEOPLE]
        prompt.write_text(prompt.read_text() + "Score generously.\n")
        with pytest.raises(RaterRefused, match="changed"):
            same_notes(outs)
        prompt.with_suffix(".json").write_text("{")
        with pytest.raises(RaterRefused, match="rater-batches wrote"):
            same_notes(outs)


class TestBeforeTheSealOpens:
    def test_sheets_that_hold_what_their_entered_runs_gave_pass(
        self, window: Path, notes: Path,
    ) -> None:
        assert ready(window, _score_every_cell(window, notes)) == []

    def test_the_practice_is_checked_too_with_no_notes(self, run: Path) -> None:
        assert ready(run, _score_every_cell(run, None)) == []

    def test_a_cell_changed_by_hand_is_refused(self, window: Path, notes: Path) -> None:
        outs = _score_every_cell(window, notes)
        sheet = sheet_path(window, "person-1")
        sheet.write_text(sheet.read_text().replace(",2,", ",1,", 1))
        with pytest.raises(RaterRefused, match="does not hold"):
            ready(window, outs)

    def test_a_sheet_not_yet_full_is_refused(self, window: Path, notes: Path) -> None:
        first = _batches(window, notes, "person-1")[0]
        _enter(window, "person-1", first, _answer(first))
        for prompt in _batches(window, notes, "person-2"):
            _enter(window, "person-2", prompt, _answer(prompt))
        with pytest.raises(RaterRefused, match="not yet scored"):
            ready(window, [_out(window, p) for p in PEOPLE])

    def test_raters_with_different_notes_are_refused(
        self, window: Path, notes: Path, tmp_path: Path,
    ) -> None:
        other = tmp_path / "other.md"
        other.write_text("N1: A heading never counts.\n")
        for person, held in zip(PEOPLE, (notes, other), strict=True):
            for prompt in _batches(window, held, person):
                _enter(window, person, prompt, _answer(prompt))
        with pytest.raises(RaterRefused, match="one set of notes"):
            ready(window, [_out(window, p) for p in PEOPLE])

    def test_what_differs_between_the_raters_runs_is_listed_for_the_result(
        self, window: Path, notes: Path,
    ) -> None:
        for person, effort in zip(PEOPLE, ("high", "low"), strict=True):
            for prompt in _batches(window, notes, person):
                _enter(window, person, prompt, _answer(prompt, effort=effort))
        (listed,) = ready(window, [_out(window, p) for p in PEOPLE])
        assert "effort" in listed and "high" in listed and "low" in listed


class TestPracticeDifferences:
    def test_the_cells_two_raters_scored_differently_are_listed(self, run: Path) -> None:
        def low(packet: str, cell: str) -> int | str:
            return 1 if cell == "C3" else _given(packet, cell)

        for person, value in (("person-1", _given), ("person-2", low)):
            for prompt in _batches(run, None, person):
                _enter(run, person, prompt, _answer(prompt, value=value))
        listed = differences(run)
        memos = [p for p in read_packets(run).packets.values()
                 if not isinstance(p, RecallPacket)]
        assert len(listed) == len(memos)
        assert all(line.endswith("C3: person-1 2, person-2 1") for line in listed)

    def test_a_scored_runs_sheets_are_never_compared(self, window: Path) -> None:
        with pytest.raises(RaterRefused, match="practice"):
            differences(window.parent)

    def test_a_folder_that_holds_no_run_is_refused(self, window: Path) -> None:
        with pytest.raises(RaterRefused, match="practice"):
            differences(window)


class TestFromTheCommandLine:
    def test_a_session_audits_its_folders_and_the_opener_checks_the_sheets(
        self, window: Path, notes: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        outs = [str(out) for out in _score_every_cell(window, notes)]
        assert command.main(["rater-audit", *outs, str(window.parent / "records")]) == 0
        assert "every run" in capsys.readouterr().out
        assert command.main(["rater-ready", str(window), *outs]) == 0
        assert "the seal may be opened" in capsys.readouterr().out
        sheet = sheet_path(window, "person-2")
        sheet.write_text(sheet.read_text().replace(",2,", ",0,", 1))
        assert command.main(["rater-ready", str(window), *outs]) == 2
        assert "does not hold" in capsys.readouterr().err

    def test_a_prompt_one_read_would_not_give_whole_gets_no_brief(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        prompt = tmp_path / "notes-propose.md"
        prompt.write_text("x\n" * LINE_LIMIT)
        assert command.main(["rater-brief", str(prompt)]) == 0
        assert capsys.readouterr().out == brief(prompt) + "\n"
        for too_long in ("x\n" * (LINE_LIMIT + 1), "x" * (PROMPT_LIMIT + 1)):
            prompt.write_text(too_long)
            assert command.main(["rater-brief", str(prompt)]) == 2
            assert "too long" in capsys.readouterr().err
        assert command.main(["rater-brief", str(tmp_path / "nowhere.md")]) == 2
        assert "no such prompt" in capsys.readouterr().err
