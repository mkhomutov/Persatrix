"""EXP-001 harness — a packet as a rater reads it (PR 6a).

Part 2 §2 of the pre-registration fixes what a rater's packet holds; PR 2
builds the packets. This is the text every rater reads, the LLM judge
included: the organisation's line, the operator's messages oldest first,
the answer key with every ID the rubric's JSON names, then the memo, or for
a recall check the key and the reply.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from pathlib import Path

import pytest

from evaluators.exp001.materials import MeetingKind, Series, load_materials
from evaluators.exp001.packets import MemoPacket, blind, memo_packet_text, recall_packet_text
from evaluators.exp001.scoring import MemoRef

MATERIALS = Path(__file__).resolve().parents[3] / "evaluators" / "experiments" / "EXP-001"


@pytest.fixture(scope="module")
def practice() -> Series:
    return load_materials(MATERIALS).practice


def _ids() -> Callable[[], str]:
    counter = itertools.count(1)
    return lambda: f"id{next(counter)}"


def _memo_packet(series: Series, meeting: str, memo: str = "## Recommendation\nB.") -> MemoPacket:
    blinded = blind([series], {MemoRef("C", series.id, meeting): memo}, {}, (), new_id=_ids())
    return blinded.memo_packets[0]


def test_a_plan_memo_packet_reads_in_order(practice: Series) -> None:
    packet = _memo_packet(practice, "practice-plan-1")
    briefing, plan = practice.meetings[0].message, practice.meetings[1].message
    assert memo_packet_text(packet) == (
        "Organisation: Harbour Players, a volunteer community theatre with a 180-seat hall\n"
        "\n"
        "--- Operator message 1 of 2 ---\n"
        f"{briefing.rstrip()}\n"
        "\n"
        "--- Operator message 2 of 2, the request the memo answers ---\n"
        f"{plan.rstrip()}\n"
        "\n"
        "--- Answer key ---\n"
        "Problems inside the plan:\n"
        "K1: The sponsors' $3 000 is counted twice.\n"
        "K2: A sell-out is assumed with no evidence.\n"
        "K3: There is no cost for the performing rights to well-known show tunes.\n"
        "Earlier facts that bear on the decision:\n"
        "F1: The hall is booked every Saturday in May, so 9 May is not available.\n"
        "Must state:\n"
        "- the hall is booked every Saturday in May\n"
        "F2: The board will not approve $40 tickets; the cap is $25.\n"
        "Must state:\n"
        "- ticket prices are capped at $25\n"
        "Options those facts make unsound: A\n"
        "\n"
        "--- Memo ---\n"
        "## Recommendation\n"
        "B.\n"
    )


def test_a_control_plans_key_says_it_lists_no_earlier_facts(practice: Series) -> None:
    control = next(m for m in practice.meetings if m.kind is MeetingKind.CONTROL)
    text = memo_packet_text(_memo_packet(practice, control.id))
    assert "Earlier facts that bear on the decision: none.\n" in text
    assert "Options those facts make unsound: none.\n" in text
    assert "--- Operator message 3 of 3, the request the memo answers ---\n" in text


def test_the_memo_is_the_packets_own_redacted_and_cut_text(practice: Series) -> None:
    packet = _memo_packet(practice, "practice-plan-1", "word " * 450)
    text = memo_packet_text(packet)
    assert text.endswith("--- Memo ---\n" + packet.memo.rstrip() + "\n")
    assert text.count("word") == 400


def test_a_recall_packet_maps_each_key_to_its_question(practice: Series) -> None:
    recall = practice.meetings[-1]
    blinded = blind(
        [practice], {}, {MemoRef("C", practice.id, recall.id): "1. May.\n2. $25.\n3. Two."}, (),
        new_id=_ids(),
    )
    assert recall_packet_text(blinded.recall_packets[0]) == (
        "Organisation: Harbour Players, a volunteer community theatre with a 180-seat hall\n"
        "\n"
        "--- Answer key ---\n"
        "R1, question 1: Every Saturday in May, which the school district has booked.\n"
        "Must include:\n"
        "- Saturdays in May\n"
        "R2, question 2: $25.\n"
        "Must include:\n"
        "- $25\n"
        "R3, question 3: Two.\n"
        "Must include:\n"
        "- two\n"
        "\n"
        "--- Reply ---\n"
        "1. May.\n"
        "2. $25.\n"
        "3. Two.\n"
    )


def test_every_scored_packet_names_every_key_id() -> None:
    """The judge's JSON names problems and facts by the key's IDs, so the
    packet must show each one."""
    for series in load_materials(MATERIALS).series:
        for meeting in series.scored_meetings:
            assert meeting.plan_key is not None
            text = memo_packet_text(_memo_packet(series, meeting.id))
            for kid in (*meeting.plan_key.problems, *meeting.plan_key.earlier_facts):
                assert f"\n{kid}: " in text
