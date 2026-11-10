"""EXP-001 harness — the AI raters that take the two people's place.

On 2026-11-10 the maintainer decided that two AI raters, not two people,
score EXP-001 (docs/experiments/EXP-001-deviation-ai-raters.md). Each
rater's own session briefs it with :mod:`evaluators.exp001.ai_raters`: it
splits the rater's packet file into batches, each written as one prompt
with the rubric, the practice notes and the batch's packets, without the
session reading a packet; then it enters each run's answer into the
rater's sheet exactly as given, refusing anything else.

These tests use a practice run, whose packets anyone may read.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from evaluators.exp001 import __main__ as command
from evaluators.exp001.ai_raters import (
    BATCH_SIZE,
    ENTERED,
    RaterRefused,
    differences,
    enter,
    split,
    write_batches,
)
from evaluators.exp001.judge import packet_kind
from evaluators.exp001.materials import MeetingKind, load_materials
from evaluators.exp001.packets import RecallPacket
from evaluators.exp001.rating import read_packets
from evaluators.exp001.scoring_run import OPENED, Progress, score
from evaluators.exp001.sheets import read_sheet, sheet_path

from ._exp001_judge_test_helpers import PROMPTS
from ._exp001_run_test_helpers import EXP
from ._exp001_scores_test_helpers import build_practice

RUBRIC = EXP / "rubric.yaml"
MODEL = "claude-fable-5-2"


@pytest.fixture(scope="module")
def _held(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One practice run with its blank sheets, built once for the module."""
    root = tmp_path_factory.mktemp("practice") / "run"
    asyncio.run(build_practice(root))
    score(root, materials=load_materials(EXP), prompts=PROMPTS)
    return root


@pytest.fixture
def run(_held: Path, tmp_path: Path) -> Path:
    return Path(shutil.copytree(_held, tmp_path / "run"))


@pytest.fixture
def notes(tmp_path: Path) -> Path:
    path = tmp_path / "notes.md"
    path.write_text("N1: A problem named only in a heading still counts.\n")
    return path


def _batches(run: Path, notes: Path | None, person: str = "person-1") -> list[Path]:
    return write_batches(run, person, run.parent / f"batches-{person}", rubric=RUBRIC,
                         notes=notes)


def _sidecar(prompt: Path) -> dict[str, Any]:
    return dict(json.loads(prompt.with_suffix(".json").read_text()))


def _given(packet: str, cell: str) -> int | str:
    return "right" if cell.startswith("R") else 2


def _answer(
    prompt: Path, *, model: str = MODEL, value: Callable[[str, str], Any] = _given,
    edit: Callable[[dict[str, dict[str, Any]]], None] | None = None,
) -> Path:
    asked = _sidecar(prompt)["cells"]
    scores = {p: {c: value(p, c) for c in cells} for p, cells in asked.items()}
    if edit is not None:
        edit(scores)
    path = prompt.with_suffix(".answer.md")
    path.write_text(f"model: {model}\n\nEvery packet, with a reason each.\n\n"
                    f"```json\n{json.dumps(scores)}\n```\n")
    return path


def _first_memo(scores: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return next(cells for cells in scores.values() if "C1" in cells)


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
        self, run: Path, notes: Path,
    ) -> None:
        prompts = _batches(run, notes)
        order = [p.id for p in read_packets(run).order("person-1")]
        assert [p.name for p in prompts] == ["batch-01.md", "batch-02.md"]
        asked = [pid for prompt in prompts for pid in _sidecar(prompt)["packets"]]
        assert asked == order and len(_sidecar(prompts[0])["packets"]) == BATCH_SIZE
        text = prompts[1].read_text()
        assert f"=== Packet {order[BATCH_SIZE]}, {BATCH_SIZE + 1} of {len(order)} ===" in text
        assert f"=== Packet {order[0]}," not in text

    def test_a_prompt_holds_the_rubric_the_notes_and_the_cells_to_score(
        self, run: Path, notes: Path,
    ) -> None:
        prompt = _batches(run, notes)[0]
        text = prompt.read_text()
        assert "C1: Finds the plan's own problems" in text
        assert "An answer is right when it gives every item" in text
        assert "N1: A problem named only in a heading still counts." in text
        drawn = read_packets(run)
        for pid, cells in _sidecar(prompt)["cells"].items():
            packet = drawn.packets[pid]
            expected = (sorted(packet.key) if isinstance(packet, RecallPacket)
                        else ["C1", "C3", "C4", "C5"]
                        if packet_kind(packet) is MeetingKind.CONTROL
                        else ["C1", "C2", "C3", "C4", "C5"])
            assert cells == expected, pid
            assert f"Cells to score for {pid}: {', '.join(cells)}" in text

    def test_with_no_notes_yet_the_prompt_says_so(self, run: Path) -> None:
        assert "None yet: this is the practice." in _batches(run, None)[0].read_text()

    def test_batches_are_written_once(self, run: Path, notes: Path) -> None:
        _batches(run, notes)
        with pytest.raises(RaterRefused, match="already"):
            _batches(run, notes)

    def test_a_sheet_that_already_holds_scores_is_refused(self, run: Path, notes: Path) -> None:
        first = _batches(run, notes)[0]
        enter(run, "person-1", first, _answer(first))
        shutil.rmtree(first.parent)
        with pytest.raises(RaterRefused, match="already holds scores"):
            _batches(run, notes)


class TestEnteringAnAnswer:
    def test_an_answer_is_entered_exactly_as_given(self, run: Path, notes: Path) -> None:
        prompt = _batches(run, notes)[0]
        def value(packet: str, cell: str) -> int | str:
            return "wrong" if cell == "R1" else 1 if cell == "C4" else _given(packet, cell)

        entered = enter(run, "person-1", prompt, _answer(prompt, value=value), model=MODEL)
        asked = _sidecar(prompt)["cells"]
        assert entered == tuple(asked)
        sheet = read_sheet(sheet_path(run, "person-1"), read_packets(run), "person-1")
        for pid, cells in asked.items():
            got = sheet.recall.get(pid) or sheet.memos[pid]
            assert {c: v for c, v in got.items() if v is not None} == {
                c: (value(pid, c) == "right" if c.startswith("R") else value(pid, c))
                for c in cells
            }
        assert len(sheet.left) == 15 - BATCH_SIZE

    def test_each_entry_is_logged_with_its_model_and_hashes(self, run: Path, notes: Path) -> None:
        prompt = _batches(run, notes)[0]
        enter(run, "person-1", prompt, _answer(prompt))
        log = (prompt.parent / ENTERED).read_text().splitlines()
        (record,) = [json.loads(line) for line in log]
        assert (record["batch"], record["model"]) == ("batch-01", MODEL)
        assert record["packets"] == _sidecar(prompt)["packets"]
        assert len(record["answer_sha256"]) == len(record["prompt_sha256"]) == 64

    @pytest.mark.parametrize(("edit", "why"), [
        (lambda s: s.popitem(), "every packet"),
        (lambda s: s.update(ffff0000={"C1": 2}), "every packet"),
        (lambda s: next(iter(s.values())).popitem(), "cells"),
        (lambda s: next(iter(s.values())).update(C9=2), "cells"),
        (lambda s: _first_memo(s).update(C1=3), "not 0, 1 or 2"),
        (lambda s: _first_memo(s).update(C1="2"), "not 0, 1 or 2"),
    ])
    def test_an_answer_that_is_not_exactly_the_cells_asked_is_refused_and_enters_nothing(
        self, run: Path, notes: Path, edit: Callable[[dict[str, Any]], None], why: str,
    ) -> None:
        prompt = _batches(run, notes)[0]
        before = sheet_path(run, "person-1").read_bytes()
        with pytest.raises(RaterRefused, match=why):
            enter(run, "person-1", prompt, _answer(prompt, edit=edit))
        assert sheet_path(run, "person-1").read_bytes() == before
        assert not (prompt.parent / ENTERED).exists()

    def test_a_recall_mark_that_is_not_right_or_wrong_is_refused(
        self, run: Path, notes: Path,
    ) -> None:
        for prompt in _batches(run, notes):
            recall = [p for p, cells in _sidecar(prompt)["cells"].items() if cells[0] == "R1"]
            if recall:
                break
        with pytest.raises(RaterRefused, match="not right or wrong"):
            enter(run, "person-1", prompt,
                  _answer(prompt, edit=lambda s: s[recall[0]].update(R1="partly")))

    @pytest.mark.parametrize("text", [
        "Scores below.\n```json\n{}\n```\n",
        f"model: {MODEL}\nNo scores at all.\n",
        f"model: {MODEL}\n```json\nnot json\n```\n",
    ])
    def test_an_answer_without_a_model_line_or_a_json_block_is_refused(
        self, run: Path, notes: Path, text: str,
    ) -> None:
        prompt = _batches(run, notes)[0]
        answer = prompt.with_suffix(".answer.md")
        answer.write_text(text)
        with pytest.raises(RaterRefused):
            enter(run, "person-1", prompt, answer)

    def test_a_run_on_another_model_is_refused(self, run: Path, notes: Path) -> None:
        prompt = _batches(run, notes)[0]
        with pytest.raises(RaterRefused, match="claude-other-1"):
            enter(run, "person-1", prompt, _answer(prompt, model="claude-other-1"), model=MODEL)

    def test_a_score_is_never_entered_twice(self, run: Path, notes: Path) -> None:
        prompt = _batches(run, notes)[0]
        enter(run, "person-1", prompt, _answer(prompt))
        with pytest.raises(RaterRefused, match="already scored"):
            enter(run, "person-1", prompt, _answer(prompt))

    def test_a_changed_prompt_is_refused(self, run: Path, notes: Path) -> None:
        prompt = _batches(run, notes)[0]
        answer = _answer(prompt)
        prompt.write_text(prompt.read_text() + "\nAnd one more thing.\n")
        with pytest.raises(RaterRefused, match="changed"):
            enter(run, "person-1", prompt, answer)

    def test_nothing_is_entered_once_the_seal_is_open(self, run: Path, notes: Path) -> None:
        prompt = _batches(run, notes)[0]
        (run / OPENED).write_text("{}")
        with pytest.raises(RaterRefused, match="seal"):
            enter(run, "person-1", prompt, _answer(prompt))

    def test_two_raters_answers_fill_every_score_scoring_waits_for(
        self, run: Path, notes: Path,
    ) -> None:
        for person in ("person-1", "person-2"):
            for prompt in _batches(run, notes, person):
                enter(run, person, prompt, _answer(prompt))
        progress = score(run, materials=load_materials(EXP), prompts=PROMPTS)
        assert isinstance(progress, Progress) and progress.complete


class TestPracticeDifferences:
    def test_the_cells_two_raters_scored_differently_are_listed(
        self, run: Path, notes: Path,
    ) -> None:
        def low(packet: str, cell: str) -> int | str:
            return 1 if cell == "C3" else _given(packet, cell)

        for person, value in (("person-1", _given), ("person-2", low)):
            for prompt in _batches(run, notes, person):
                enter(run, person, prompt, _answer(prompt, value=value))
        listed = differences(run)
        memos = [p for p in read_packets(run).packets.values()
                 if not isinstance(p, RecallPacket)]
        assert len(listed) == len(memos)
        assert all(line.endswith("C3: person-1 2, person-2 1") for line in listed)

    def test_a_scored_runs_sheets_are_never_compared(self, run: Path) -> None:
        state = json.loads((run / "run.json").read_text())
        (run / "run.json").write_text(json.dumps({**state, "windows": []}))
        with pytest.raises(RaterRefused, match="practice"):
            differences(run)


class TestFromTheCommandLine:
    def test_a_raters_session_writes_its_batches_then_enters_each_answer(
        self, run: Path, notes: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        out = run.parent / "batches"
        assert command.main(["rater-batches", str(run), "person-2", str(out),
                             "--notes", str(notes)]) == 0
        prompts = sorted(out.glob("batch-*.md"))
        assert capsys.readouterr().out.split() == [str(p) for p in prompts]
        assert "N1: A problem named" in prompts[0].read_text()
        answer = _answer(prompts[0])
        assert command.main(["rater-enter", str(run), "person-2", str(prompts[0]), str(answer),
                             "--model", MODEL]) == 0
        assert f"entered {BATCH_SIZE} packets from batch-01" in capsys.readouterr().out

    def test_what_the_rules_refuse_says_why_and_fails(
        self, run: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        prompt = _batches(run, None)[0]
        answer = _answer(prompt, model="claude-other-1")
        assert command.main(["rater-enter", str(run), "person-1", str(prompt), str(answer),
                             "--model", MODEL]) == 2
        assert "claude-other-1" in capsys.readouterr().err
        assert command.main(["rater-batches", str(run), "person-1",
                             str(prompt.parent)]) == 2

    def test_practice_differences_are_printed(
        self, run: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        for person in ("person-1", "person-2"):
            for prompt in _batches(run, None, person):
                enter(run, person, prompt, _answer(prompt))
        assert command.main(["rater-differences", str(run)]) == 0
        assert "the two raters agree on every cell" in capsys.readouterr().out
