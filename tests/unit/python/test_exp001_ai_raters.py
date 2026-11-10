"""EXP-001 harness — the AI raters that take the two people's place.

On 2026-11-10 the maintainer decided that two AI raters, not two people,
score EXP-001 (docs/experiments/EXP-001-deviation-ai-raters.md). Each
rater's own session briefs it with :mod:`evaluators.exp001.ai_raters`: it
splits the rater's packet file into batches, each written as one prompt
with the rubric, the practice notes and the batch's packets, without the
session reading a packet; then it enters each run's answer into the
rater's sheet exactly as given, refusing anything else. How a run's own
record is checked, and the runs kept or never logged, is tested in
``test_exp001_ai_rater_runs.py``.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from evaluators.exp001 import __main__ as command
from evaluators.exp001 import ai_raters
from evaluators.exp001.ai_rater_runs import MODEL, brief
from evaluators.exp001.ai_raters import (
    BATCH_SIZE,
    RaterRefused,
    split,
)
from evaluators.exp001.judge import packet_kind
from evaluators.exp001.materials import MeetingKind, load_materials
from evaluators.exp001.packets import RecallPacket
from evaluators.exp001.rating import read_packets
from evaluators.exp001.scoring_run import OPENED, Progress, score
from evaluators.exp001.sheets import read_sheet, sheet_path

from ._exp001_ai_rater_test_helpers import (
    answer as _answer,
)
from ._exp001_ai_rater_test_helpers import (
    as_window,
    held_practice,
    notes_file,
    record,
)
from ._exp001_ai_rater_test_helpers import (
    batches as _batches,
)
from ._exp001_ai_rater_test_helpers import (
    enter_answer as _enter,
)
from ._exp001_ai_rater_test_helpers import (
    first_memo as _first_memo,
)
from ._exp001_ai_rater_test_helpers import (
    given as _given,
)
from ._exp001_ai_rater_test_helpers import (
    out_of as _out,
)
from ._exp001_ai_rater_test_helpers import (
    runs as _runs,
)
from ._exp001_ai_rater_test_helpers import (
    scores as _scores,
)
from ._exp001_ai_rater_test_helpers import (
    sidecar as _sidecar,
)
from ._exp001_judge_test_helpers import PROMPTS
from ._exp001_run_test_helpers import EXP


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


class TestSplittingAPacketFile:
    def test_it_splits_on_each_packets_heading_in_order(self) -> None:
        text = "=== Packet aa11, 1 of 2 ===\n\none\n=== Packet bb22, 2 of 2 ===\n\ntwo\n"
        assert split(text) == [
            ("aa11", "=== Packet aa11, 1 of 2 ===\n\none\n"),
            ("bb22", "=== Packet bb22, 2 of 2 ===\n\ntwo\n"),
        ]

    @pytest.mark.parametrize("text", [
        "=== Packet aa11, 1 of 3 ===\n\none\n=== Packet bb22, 2 of 3 ===\n\ntwo\n",
        "=== Packet aa11, 2 of 2 ===\n\none\n=== Packet bb22, 1 of 2 ===\n\ntwo\n",
        "preface\n=== Packet aa11, 1 of 1 ===\n\none\n",
        "",
    ])
    def test_a_file_whose_headings_do_not_count_up_is_refused(self, text: str) -> None:
        with pytest.raises(RaterRefused):
            split(text)


class TestWritingTheBatches:
    def test_each_batch_holds_the_next_packets_in_the_raters_own_order(
        self, window: Path, notes: Path,
    ) -> None:
        prompts = _batches(window, notes)
        order = [p.id for p in read_packets(window).order("person-1")]
        assert [p.name for p in prompts] == ["batch-01.md", "batch-02.md"]
        asked = [pid for prompt in prompts for pid in _sidecar(prompt)["packets"]]
        assert asked == order and len(_sidecar(prompts[0])["packets"]) == BATCH_SIZE
        text = prompts[1].read_text()
        assert f"=== Packet {order[BATCH_SIZE]}, {BATCH_SIZE + 1} of {len(order)} ===" in text
        assert f"=== Packet {order[0]}," not in text

    def test_a_prompt_holds_the_rubric_the_notes_and_the_cells_to_score(
        self, window: Path, notes: Path,
    ) -> None:
        prompt = _batches(window, notes)[0]
        text = prompt.read_text()
        assert "C1: Finds the plan's own problems" in text
        assert "An answer is right when it gives every item" in text
        assert "N1: A problem named only in a heading still counts." in text
        assert _sidecar(prompt)["notes_sha256"] == hashlib.sha256(notes.read_bytes()).hexdigest()
        drawn = read_packets(window)
        for pid, cells in _sidecar(prompt)["cells"].items():
            packet = drawn.packets[pid]
            expected = (sorted(packet.key) if isinstance(packet, RecallPacket)
                        else ["C1", "C3", "C4", "C5"]
                        if packet_kind(packet) is MeetingKind.CONTROL
                        else ["C1", "C2", "C3", "C4", "C5"])
            assert cells == expected, pid
            assert f"Cells to score for {pid}: {', '.join(cells)}" in text

    def test_a_practice_prompt_says_there_are_no_notes_yet(self, run: Path) -> None:
        prompt = _batches(run, None)[0]
        assert "None yet: this is the practice." in prompt.read_text()
        assert _sidecar(prompt)["notes_sha256"] is None

    def test_the_practice_is_scored_with_no_notes(self, run: Path, notes: Path) -> None:
        with pytest.raises(RaterRefused, match="practice"):
            _batches(run, notes)

    def test_a_scored_window_is_scored_only_with_the_notes(self, window: Path) -> None:
        with pytest.raises(RaterRefused, match="--notes"):
            _batches(window, None)

    def test_a_folder_that_is_neither_is_refused(self, window: Path, notes: Path) -> None:
        (window.parent / "run.json").unlink()
        with pytest.raises(RaterRefused, match="neither"):
            _batches(window, notes)

    def test_a_batch_holds_fewer_packets_when_eight_would_not_fit_one_read(
        self, window: Path, notes: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(ai_raters, "PROMPT_LIMIT", 15_000)
        prompts = _batches(window, notes)
        assert len(prompts) > 2
        assert all(len(p.read_text()) <= 15_000 for p in prompts)
        asked = [pid for prompt in prompts for pid in _sidecar(prompt)["packets"]]
        assert asked == [p.id for p in read_packets(window).order("person-1")]

    def test_a_packet_too_long_for_one_read_alone_is_refused(
        self, window: Path, notes: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(ai_raters, "PROMPT_LIMIT", 1_000)
        with pytest.raises(RaterRefused, match="too long"):
            _batches(window, notes)
        assert not any(_out(window, "person-1").glob("batch-*"))

    def test_batches_are_written_once(self, window: Path, notes: Path) -> None:
        _batches(window, notes)
        with pytest.raises(RaterRefused, match="already"):
            _batches(window, notes)

    def test_a_sheet_that_already_holds_scores_is_refused(
        self, window: Path, notes: Path,
    ) -> None:
        first = _batches(window, notes)[0]
        _enter(window, "person-1", first, _answer(first))
        shutil.rmtree(first.parent)
        with pytest.raises(RaterRefused, match="already holds scores"):
            _batches(window, notes)


class TestEnteringAnAnswer:
    def test_an_answer_is_entered_exactly_as_given(self, window: Path, notes: Path) -> None:
        prompt = _batches(window, notes)[0]
        def value(packet: str, cell: str) -> int | str:
            return "wrong" if cell == "R1" else 1 if cell == "C4" else _given(packet, cell)

        entered = _enter(window, "person-1", prompt, _answer(prompt, value=value))
        asked = _sidecar(prompt)["cells"]
        assert entered == tuple(asked)
        sheet = read_sheet(sheet_path(window, "person-1"), read_packets(window), "person-1")
        for pid, cells in asked.items():
            got = sheet.recall.get(pid) or sheet.memos[pid]
            assert {c: v for c, v in got.items() if v is not None} == {
                c: (value(pid, c) == "right" if c.startswith("R") else value(pid, c))
                for c in cells
            }
        assert len(sheet.left) == 15 - BATCH_SIZE

    def test_each_run_is_logged_with_its_model_and_hashes(
        self, window: Path, notes: Path,
    ) -> None:
        prompt = _batches(window, notes)[0]
        run_record = _answer(prompt)
        _enter(window, "person-1", prompt, run_record)
        (logged,) = _runs(prompt.parent)
        assert (logged["batch"], logged["run"], logged["outcome"]) == ("batch-01", 1, "entered")
        assert (logged["model"], logged["record"]) == (MODEL, str(run_record))
        assert logged["agent"] == json.loads(run_record.read_text().split("\n")[0])["agentId"]
        assert logged["packets"] == _sidecar(prompt)["packets"]
        assert [f["type"] for f in logged["instructions"]] == ["User"]
        assert all(len(logged[f"{k}_sha256"]) == 64 for k in ("prompt", "record", "answer"))

    def test_the_record_and_its_answer_are_kept_beside_the_prompt(
        self, window: Path, notes: Path,
    ) -> None:
        prompt = _batches(window, notes)[0]
        run_record = _answer(prompt)
        _enter(window, "person-1", prompt, run_record)
        assert (prompt.parent / "batch-01.run-1.jsonl").read_bytes() == run_record.read_bytes()
        kept = (prompt.parent / "batch-01.run-1.answer.md").read_text()
        assert kept.startswith(f"model: {MODEL}\n") and "```json" in kept

    def test_a_refused_run_is_logged_with_why_and_kept_then_held_again(
        self, window: Path, notes: Path,
    ) -> None:
        prompt = _batches(window, notes)[0]
        refused = _answer(prompt, model="claude-other-1")
        with pytest.raises(RaterRefused):
            _enter(window, "person-1", prompt, refused)
        assert (prompt.parent / "batch-01.run-1.jsonl").read_bytes() == refused.read_bytes()
        _enter(window, "person-1", prompt, _answer(prompt))
        first, second = _runs(prompt.parent)
        assert (first["outcome"], second["outcome"]) == ("refused", "entered")
        assert "claude-other-1" in first["why"] and second["run"] == 2

    def test_an_answer_given_as_the_runs_last_words_is_read_too(
        self, window: Path, notes: Path,
    ) -> None:
        prompt = _batches(window, notes)[0]
        assert _enter(window, "person-1", prompt, _answer(prompt, handback=False))

    @pytest.mark.parametrize(("edit", "why"), [
        (lambda s: s.popitem(), "every packet"),
        (lambda s: s.update(ffff0000={"C1": 2}), "every packet"),
        (lambda s: next(iter(s.values())).popitem(), "cells"),
        (lambda s: next(iter(s.values())).update(C9=2), "cells"),
        (lambda s: _first_memo(s).update(C1=3), "not 0, 1 or 2"),
        (lambda s: _first_memo(s).update(C1="2"), "not 0, 1 or 2"),
    ])
    def test_an_answer_that_is_not_exactly_the_cells_asked_is_refused_and_enters_nothing(
        self, window: Path, notes: Path, edit: Callable[[dict[str, Any]], None], why: str,
    ) -> None:
        prompt = _batches(window, notes)[0]
        before = sheet_path(window, "person-1").read_bytes()
        with pytest.raises(RaterRefused, match=why):
            _enter(window, "person-1", prompt, _answer(prompt, edit=edit))
        assert sheet_path(window, "person-1").read_bytes() == before
        assert [r["outcome"] for r in _runs(prompt.parent)] == ["refused"]

    def test_a_recall_mark_that_is_not_right_or_wrong_is_refused(
        self, window: Path, notes: Path,
    ) -> None:
        for prompt in _batches(window, notes):
            recall = [p for p, cells in _sidecar(prompt)["cells"].items() if cells[0] == "R1"]
            if recall:
                break
        with pytest.raises(RaterRefused, match="not right or wrong"):
            _enter(window, "person-1", prompt,
                   _answer(prompt, edit=lambda s: s[recall[0]].update(R1="partly")))

    @pytest.mark.parametrize(("shape", "why"), [
        (lambda block, pid: block.replace(f'"{pid}": ', f'"{pid}": {{"C1": 0}}, "{pid}": ', 1),
         "twice"),
        (lambda block, pid: block.replace('"C1": 2', '"C1": 2, "C1": 0', 1), "twice"),
        (lambda block, pid: f"```json\n{{}}\n```\n\n{block}", "more than one"),
        (lambda block, pid: f"{block}\nOn reflection, C1 is 1.\n", "after"),
    ])
    def test_an_answer_that_could_be_read_two_ways_is_refused(
        self, window: Path, notes: Path, shape: Callable[[str, str], str], why: str,
    ) -> None:
        prompt = _batches(window, notes)[0]
        scores = _scores(prompt)
        block = f"```json\n{json.dumps(scores)}\n```\n"
        text = f"model: {MODEL}\n\nReasons.\n\n{shape(block, next(iter(scores)))}"
        with pytest.raises(RaterRefused, match=why):
            _enter(window, "person-1", prompt, record(prompt, text))

    @pytest.mark.parametrize("text", [
        "Scores below.\n```json\n{}\n```\n",
        f"model: {MODEL}\nNo scores at all.\n",
        f"model: {MODEL}\n```json\nnot json\n```\n",
    ])
    def test_an_answer_without_a_model_line_or_a_json_block_is_refused(
        self, window: Path, notes: Path, text: str,
    ) -> None:
        prompt = _batches(window, notes)[0]
        with pytest.raises(RaterRefused):
            _enter(window, "person-1", prompt, record(prompt, text))

    def test_the_model_is_fable_5_2_and_a_run_on_another_is_refused(
        self, window: Path, notes: Path,
    ) -> None:
        assert MODEL == "claude-fable-5-2"
        prompt = _batches(window, notes)[0]
        with pytest.raises(RaterRefused, match="claude-other-1"):
            _enter(window, "person-1", prompt, _answer(prompt, model="claude-other-1"))

    def test_a_run_that_only_says_it_ran_on_the_model_is_refused(
        self, window: Path, notes: Path,
    ) -> None:
        prompt = _batches(window, notes)[0]
        claimed = _answer(prompt)
        claimed.write_text(claimed.read_text().replace(  # the run's first reply
            f'"message": {{"model": "{MODEL}"', '"message": {"model": "claude-fable-5-1"', 1))
        with pytest.raises(RaterRefused, match="claude-fable-5-1"):
            _enter(window, "person-1", prompt, claimed)

    @pytest.mark.parametrize("calls", [
        [],
        [("Read", {"file_path": "/elsewhere/seal.json"})],
        [("Read", {"file_path": "PROMPT"}), ("Bash", {"command": "ls"})],
        [("Read", {"file_path": "PROMPT"}), ("Read", {"file_path": "PROMPT"})],
    ])
    def test_a_run_that_did_anything_but_read_its_prompt_is_refused(
        self, window: Path, notes: Path, calls: list[tuple[str, dict[str, Any]]],
    ) -> None:
        prompt = _batches(window, notes)[0]
        made = [(name, {k: str(prompt) if v == "PROMPT" else v for k, v in given.items()})
                for name, given in calls]
        with pytest.raises(RaterRefused, match="tool"):
            _enter(window, "person-1", prompt, _answer(prompt, calls=made))
        assert [r["outcome"] for r in _runs(prompt.parent)] == ["refused"]

    def test_a_score_is_never_entered_twice(self, window: Path, notes: Path) -> None:
        prompt = _batches(window, notes)[0]
        _enter(window, "person-1", prompt, _answer(prompt))
        with pytest.raises(RaterRefused, match="already scored"):
            _enter(window, "person-1", prompt, _answer(prompt))

    def test_a_changed_prompt_is_refused(self, window: Path, notes: Path) -> None:
        prompt = _batches(window, notes)[0]
        answer = _answer(prompt)
        prompt.write_text(prompt.read_text() + "\nAnd one more thing.\n")
        with pytest.raises(RaterRefused, match="changed"):
            _enter(window, "person-1", prompt, answer)

    def test_a_prompt_rater_batches_did_not_write_is_refused(
        self, window: Path, notes: Path,
    ) -> None:
        prompt = _batches(window, notes)[0]
        answer = _answer(prompt)
        prompt.with_suffix(".json").unlink()
        with pytest.raises(RaterRefused, match="rater-batches"):
            _enter(window, "person-1", prompt, answer)

    def test_a_packet_missing_from_the_sheet_is_refused(self, window: Path, notes: Path) -> None:
        prompt = _batches(window, notes)[0]
        answer = _answer(prompt)
        sheet = sheet_path(window, "person-1")
        first = _sidecar(prompt)["packets"][0]
        sheet.write_text("".join(line for line in sheet.read_text().splitlines(keepends=True)
                                 if f",{first}," not in line))
        with pytest.raises(RaterRefused, match=first):
            _enter(window, "person-1", prompt, answer)

    def test_nothing_is_entered_once_the_seal_is_open(self, window: Path, notes: Path) -> None:
        prompt = _batches(window, notes)[0]
        (window / OPENED).write_text("{}")
        with pytest.raises(RaterRefused, match="seal"):
            _enter(window, "person-1", prompt, _answer(prompt))

    def test_two_entries_at_once_both_land(
        self, window: Path, notes: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        first, second = _batches(window, notes)
        answers = (_answer(first), _answer(second))
        inside, go = threading.Event(), threading.Event()
        real = ai_raters._read_sheet

        def slow(directory: Path, person: str) -> tuple[list[str], list[list[str]]]:
            read = real(directory, person)
            if not inside.is_set():
                inside.set()
                go.wait(5)
            return read

        monkeypatch.setattr(ai_raters, "_read_sheet", slow)
        done: list[tuple[str, ...]] = []
        one = threading.Thread(target=lambda: done.append(
            _enter(window, "person-1", first, answers[0])))
        one.start()
        assert inside.wait(5)
        two = threading.Thread(target=lambda: done.append(
            _enter(window, "person-1", second, answers[1])))
        two.start()
        two.join(0.3)
        waited = two.is_alive()
        go.set()
        one.join(5)
        two.join(5)
        assert waited, "the second entry did not wait for the first"
        assert len(done) == 2
        sheet = read_sheet(sheet_path(window, "person-1"), read_packets(window), "person-1")
        assert not sheet.left

    def test_two_raters_answers_fill_every_score_scoring_waits_for(self, run: Path) -> None:
        for person in ("person-1", "person-2"):
            for prompt in _batches(run, None, person):
                _enter(run, person, prompt, _answer(prompt))
        progress = score(run, materials=load_materials(EXP), prompts=PROMPTS)
        assert isinstance(progress, Progress) and progress.complete


class TestFromTheCommandLine:
    def test_a_raters_session_writes_its_batches_briefs_each_run_then_enters_it(
        self, window: Path, notes: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        out = window.parent / "batches"
        assert command.main(["rater-batches", str(window), "person-2", str(out),
                             "--notes", str(notes)]) == 0
        prompts = sorted(out.glob("batch-*.md"))
        assert capsys.readouterr().out.split() == [str(p) for p in prompts]
        assert command.main(["rater-brief", str(prompts[0])]) == 0
        assert capsys.readouterr().out == brief(prompts[0]) + "\n"
        answer = _answer(prompts[0])
        assert command.main(["rater-enter", str(window), "person-2", str(prompts[0]),
                             str(answer)]) == 0
        assert f"entered {BATCH_SIZE} packets from batch-01" in capsys.readouterr().out
        assert command.main(["rater-audit", str(out), str(answer.parent)]) == 0
        assert "every run" in capsys.readouterr().out

    def test_what_the_rules_refuse_says_why_and_fails(
        self, window: Path, notes: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        prompt = _batches(window, notes)[0]
        answer = _answer(prompt, model="claude-other-1")
        assert command.main(["rater-enter", str(window), "person-1", str(prompt),
                             str(answer)]) == 2
        assert "claude-other-1" in capsys.readouterr().err
        assert command.main(["rater-batches", str(window), "person-1", str(prompt.parent),
                             "--notes", str(notes)]) == 2
        forgotten = _answer(prompt)
        assert command.main(["rater-audit", str(prompt.parent), str(forgotten.parent)]) == 2
        assert str(forgotten) in capsys.readouterr().out

    def test_a_notes_run_is_kept_and_the_notes_compared(
        self, window: Path, notes: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        prompt = tmp_path / "notes" / "notes-vote.md"
        prompt.parent.mkdir()
        prompt.write_text("Accept or reject each note.\n")
        assert command.main(["rater-keep", str(prompt),
                             str(record(prompt, f"model: {MODEL}\n\nAll accepted.\n"))]) == 0
        assert "notes-vote.run-1.answer.md" in capsys.readouterr().out
        for person in ("person-1", "person-2"):
            _batches(window, notes, person)
        assert command.main(["rater-same-notes", *(str(_out(window, p))
                                                   for p in ("person-1", "person-2"))]) == 0
        assert hashlib.sha256(notes.read_bytes()).hexdigest() in capsys.readouterr().out

    def test_practice_differences_are_printed(
        self, run: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        for person in ("person-1", "person-2"):
            for prompt in _batches(run, None, person):
                _enter(run, person, prompt, _answer(prompt))
        assert command.main(["rater-differences", str(run)]) == 0
        assert "the two raters agree on every cell" in capsys.readouterr().out
