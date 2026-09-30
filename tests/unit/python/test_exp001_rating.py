"""EXP-001 harness — the packets each rater scores, drawn once and kept (PR 6b).

Once a run's pairs have all been held, the harness gathers every answer the
meetings asked for, blinds them into packets and draws each rater's order:
the two people's and the judge's. They are drawn once and kept, so a run
started again finds the same packet IDs in the same orders, and the judge
is asked exactly what it was asked before. The seal, from packet ID back to
arm, is kept apart. How a packet is blinded is in ``test_exp001_packets.py``.
"""

from __future__ import annotations

import itertools
import json
import random
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from evaluators.exp001.attempts import Held, SeriesRun, Try
from evaluators.exp001.materials import load_series
from evaluators.exp001.packets import (
    ADVISER_PLACEHOLDER,
    MemoPacket,
    RecallPacket,
    load_adviser_names,
    memo_packet_text,
    recall_packet_text,
)
from evaluators.exp001.pairs import Kept
from evaluators.exp001.rating import (
    PACKETS,
    PEOPLE,
    RATERS,
    SEAL,
    SEALED,
    Answers,
    draw_packets,
    gather_answers,
    read_packets,
    read_seal,
)
from evaluators.exp001.scoring import MemoRef

_EXP = Path(__file__).resolve().parents[3] / "evaluators" / "experiments" / "EXP-001"
SERIES = load_series(_EXP / "practice.yaml")
BRIEFING, PLAN, CONTROL, RECALL = SERIES.meetings
NAMES = load_adviser_names(_EXP / "panel.yaml")


def _counter_ids(start: int = 1) -> Callable[[], str]:
    counter = itertools.count(start)
    return lambda: f"{next(counter):08x}"


def _pair(arm: str, answers: dict[str, str | None], *, dropped: bool = False) -> SeriesRun[Kept]:
    """An arm's practice series as kept: *answers* by meeting; a meeting not
    named left no record at all, as when its orchestrator exited."""
    tries = tuple(
        Try(m.id, 1, 1, Held(Kept(answers[m.id], {"meeting": m.id}) if m.id in answers else None))
        for m in SERIES.meetings
    )
    return SeriesRun(arm, SERIES.id, tries, None if dropped else 1)


# A chair's message can end in a line break; the packet keeps its memo as written.
_ANSWERED = {BRIEFING.id: None, PLAN.id: "Plan memo.\n", CONTROL.id: "Control memo.",
             RECALL.id: "R1: the lease."}


def _answers() -> Answers:
    return gather_answers([_pair("A", _ANSWERED), _pair("C", _ANSWERED)], {SERIES.id: SERIES})


def _draw(directory: Path, answers: Answers | None = None, *, seed: int = 1) -> Any:
    return draw_packets(
        directory, [SERIES], answers or _answers(), NAMES, new_id=_counter_ids(),
        rng=random.Random(seed),
    )


class TestGatheringTheAnswers:
    def test_each_memo_and_each_recall_checks_answers_are_gathered(self) -> None:
        answers = gather_answers([_pair("C", _ANSWERED)], {SERIES.id: SERIES})
        assert answers.memos == {
            MemoRef("C", SERIES.id, PLAN.id): "Plan memo.\n",
            MemoRef("C", SERIES.id, CONTROL.id): "Control memo.",
        }
        assert answers.recall_replies == {MemoRef("C", SERIES.id, RECALL.id): "R1: the lease."}
        assert answers.missing == ()

    def test_a_missing_answer_goes_to_no_rater_and_is_listed(self) -> None:
        """A missing memo scores 0 without reaching the raters (part 2 §2)."""
        answers = gather_answers(
            [_pair("B", {BRIEFING.id: None, PLAN.id: None, RECALL.id: "R1."})],
            {SERIES.id: SERIES},
        )
        assert answers.memos == {}
        assert answers.missing == (
            MemoRef("B", SERIES.id, PLAN.id), MemoRef("B", SERIES.id, CONTROL.id),
        )

    def test_the_briefing_asks_for_no_answer(self) -> None:
        answers = gather_answers([_pair("A", {**_ANSWERED, BRIEFING.id: "Noted."})],
                                 {SERIES.id: SERIES})
        assert BRIEFING.id not in {ref.meeting for ref in (*answers.memos, *answers.missing)}

    def test_a_dropped_series_goes_to_no_rater(self) -> None:
        """It is dropped from every arm's comparisons, so nothing of it is scored."""
        answers = gather_answers(
            [_pair("C", _ANSWERED, dropped=True), _pair("D", _ANSWERED)], {SERIES.id: SERIES},
        )
        assert {ref.arm for ref in (*answers.memos, *answers.recall_replies)} == {"D"}
        assert answers.missing == ()


class TestDrawingThePackets:
    def test_every_answer_becomes_a_packet_in_each_raters_order(self, tmp_path: Path) -> None:
        drawn = _draw(tmp_path)
        assert set(drawn.orders) == set(RATERS) == {*PEOPLE, "judge"}
        assert len(drawn.packets) == 6  # two arms: two memos and a recall check each
        for rater in RATERS:
            assert sorted(drawn.orders[rater]) == sorted(drawn.packets)
        assert len({tuple(order) for order in drawn.orders.values()}) == len(RATERS)
        assert [p.id for p in drawn.order("judge")] == drawn.orders["judge"]

    def test_each_person_reads_their_packets_in_their_own_order(self, tmp_path: Path) -> None:
        drawn = _draw(tmp_path)
        for person in PEOPLE:
            text = (tmp_path / "raters" / f"{person}.txt").read_text()
            ids = drawn.orders[person]
            assert [text.index(f"Packet {pid}") for pid in ids] == sorted(
                text.index(f"Packet {pid}") for pid in ids
            )
            for packet in drawn.order(person):
                shown = (memo_packet_text(packet) if isinstance(packet, MemoPacket)
                         else recall_packet_text(packet))
                assert shown in text
        assert not (tmp_path / "raters" / "judge.txt").exists()

    def test_the_seal_is_kept_apart_with_the_missing_answers_and_the_cuts(
        self, tmp_path: Path,
    ) -> None:
        missing = gather_answers(
            [_pair("C", _ANSWERED), _pair("B", {**_ANSWERED, CONTROL.id: None})],
            {SERIES.id: SERIES},
        )
        drawn = _draw(tmp_path, missing)
        assert (tmp_path / SEALED / SEAL).is_file()
        seal = read_seal(tmp_path)
        assert set(seal.packets) == set(drawn.packets)
        assert set(seal.packets.values()) == {*missing.memos, *missing.recall_replies}
        assert seal.missing == (MemoRef("B", SERIES.id, CONTROL.id),)
        assert {(ref.arm, ref.meeting): (cut.words, cut.cut) for ref, cut in seal.cuts.items()} == {
            ("C", PLAN.id): (2, False), ("C", CONTROL.id): (2, False),
            ("B", PLAN.id): (2, False),
        }

    def test_the_packets_name_no_arm(self, tmp_path: Path) -> None:
        _draw(tmp_path)
        kept = json.loads((tmp_path / PACKETS).read_text())
        assert set(kept) == {"packets", "orders"}
        assert all(set(p) <= {"id", "kind", "organisation", "messages", "key", "memo", "reply"}
                   for p in kept["packets"])

    def test_adviser_names_never_reach_a_rater(self, tmp_path: Path) -> None:
        chair = NAMES[1]  # the first adviser's display name
        answers = gather_answers([_pair("C", {**_ANSWERED, PLAN.id: f"{chair} says no."})],
                                 {SERIES.id: SERIES})
        _draw(tmp_path, answers)
        for person in PEOPLE:
            text = (tmp_path / "raters" / f"{person}.txt").read_text()
            assert chair not in text and f"{ADVISER_PLACEHOLDER} says no." in text


class TestDrawnOnce:
    def test_a_second_draw_reads_back_the_first(self, tmp_path: Path) -> None:
        """A second draw would give new IDs in new orders."""
        first = _draw(tmp_path, seed=1)
        again = draw_packets(tmp_path, [SERIES], _answers(), NAMES,
                             new_id=_counter_ids(start=0x1000), rng=random.Random(99))
        assert again == first

    def test_the_packets_read_back_are_the_packets_drawn(self, tmp_path: Path) -> None:
        """So the judge is asked exactly what it was asked before."""
        drawn = _draw(tmp_path)
        assert read_packets(tmp_path) == drawn
        kinds = {type(p) for p in drawn.packets.values()}
        assert kinds == {MemoPacket, RecallPacket}

    def test_answers_changed_since_the_draw_are_refused(self, tmp_path: Path) -> None:
        _draw(tmp_path)
        changed = gather_answers([_pair("C", _ANSWERED)], {SERIES.id: SERIES})
        with pytest.raises(ValueError, match="changed since the packets were drawn"):
            _draw(tmp_path, changed)

    def test_a_draw_cut_short_before_its_packets_were_kept_is_drawn_again(
        self, tmp_path: Path,
    ) -> None:
        """The packets file is written last: without it nothing was drawn."""
        (tmp_path / "raters").mkdir()
        (tmp_path / "raters" / "person-1.txt").write_text("half written")
        drawn = _draw(tmp_path)
        assert f"Packet {drawn.orders['person-1'][0]}" in (
            tmp_path / "raters" / "person-1.txt"
        ).read_text()
