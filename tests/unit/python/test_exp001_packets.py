"""EXP-001 harness — blinded rater packets.

Part 2 §2 of the pre-registration fixes what a rater's packet holds (the
organisation's line, every operator message up to and including the plan,
the answer key, the memo) and how the raters are kept blind: adviser names
removed, a random ID per memo, each rater's packets in a different order,
and the map from ID to arm sealed until every score is in.
"""

from __future__ import annotations

import itertools
import random
from collections.abc import Callable
from pathlib import Path

import pytest

from evaluators.exp001.materials import Series, load_materials
from evaluators.exp001.packets import (
    ADVISER_PLACEHOLDER,
    blind,
    load_adviser_names,
    rater_orders,
    redact,
)
from evaluators.exp001.scoring import WORD_LIMIT, MemoRef

MATERIALS = Path(__file__).resolve().parents[3] / "evaluators" / "experiments" / "EXP-001"
NAMES = ("Lunar Stoat", "lunar-stoat", "Velvet Pika", "velvet-pika")


@pytest.fixture(scope="module")
def series_1() -> Series:
    return load_materials(MATERIALS).series[0]


def _counter_ids() -> Callable[[], str]:
    counter = itertools.count(1)
    return lambda: f"id{next(counter)}"


def test_load_adviser_names_reads_every_id_and_name_from_the_panel() -> None:
    names = load_adviser_names(MATERIALS / "panel.yaml")
    assert {"lunar-stoat", "Lunar Stoat", "crimson-crow", "Crimson Crow"} <= set(names)
    assert "operator" not in names


@pytest.mark.parametrize(
    "text",
    [
        "Lunar Stoat recommends B.",
        "lunar-stoat recommends B.",
        "LUNAR STOAT recommends B.",
        "Lunar-Stoat recommends B.",
        "Lunar\nStoat recommends B.",
    ],
)
def test_redact_removes_an_adviser_name_however_it_is_written(text: str) -> None:
    assert redact(text, NAMES) == f"{ADVISER_PLACEHOLDER} recommends B."


def test_redact_leaves_other_words_alone() -> None:
    text = "The lunar calendar and a stoat-proof store; Velvet Pika's view differs."
    assert redact(text, NAMES) == (
        f"The lunar calendar and a stoat-proof store; {ADVISER_PLACEHOLDER}'s view differs."
    )


def test_packet_holds_the_organisation_every_message_so_far_the_key_and_the_memo(
    series_1: Series,
) -> None:
    ref = MemoRef("D", "series-1", "series-1-plan-2")
    blinded = blind([series_1], {ref: "Recommend B. Lunar Stoat agrees."}, {}, NAMES)

    (packet,) = blinded.memo_packets
    meetings = {m.id: m for m in series_1.meetings}
    assert packet.organisation == series_1.organisation
    assert packet.messages == tuple(
        meetings[m].message for m in ("series-1-briefing", "series-1-plan-1", "series-1-plan-2")
    )
    assert packet.key == meetings["series-1-plan-2"].plan_key
    assert packet.memo == f"Recommend B. {ADVISER_PLACEHOLDER} agrees."


def test_packet_memo_is_cut_at_400_words_and_the_cut_is_recorded(series_1: Series) -> None:
    ref = MemoRef("A", "series-1", "series-1-plan-1")
    blinded = blind([series_1], {ref: "word " * 450}, {}, NAMES)

    assert len(blinded.memo_packets[0].memo.split()) == WORD_LIMIT
    assert (blinded.cuts[ref].words, blinded.cuts[ref].cut) == (450, True)


def test_packets_carry_no_arm_and_the_seal_maps_ids_back(series_1: Series) -> None:
    memos = {
        MemoRef(arm, "series-1", "series-1-plan-1"): f"memo by {arm}"
        for arm in ("A", "B", "C", "D", "D-prime")
    }
    blinded = blind([series_1], memos, {}, NAMES, new_id=_counter_ids())

    assert {p.id for p in blinded.memo_packets} == set(blinded.seal)
    assert set(blinded.seal.values()) == set(memos)
    for packet in blinded.memo_packets:
        assert packet.memo == memos[blinded.seal[packet.id]]
        assert not hasattr(packet, "arm")


def test_packet_ids_come_from_the_os_random_source_by_default(series_1: Series) -> None:
    memos = {MemoRef(a, "series-1", "series-1-plan-1"): "m" for a in ("A", "B", "C")}
    first = blind([series_1], memos, {}, NAMES)
    second = blind([series_1], memos, {}, NAMES)
    assert set(first.seal).isdisjoint(second.seal)


def test_a_repeated_id_is_drawn_again(series_1: Series) -> None:
    ids = iter(["same", "same", "other"])
    memos = {MemoRef(a, "series-1", "series-1-plan-1"): "m" for a in ("A", "B")}
    blinded = blind([series_1], memos, {}, NAMES, new_id=lambda: next(ids))
    assert set(blinded.seal) == {"same", "other"}


def test_recall_packet_holds_the_key_and_the_redacted_reply(series_1: Series) -> None:
    ref = MemoRef("C", "series-1", "series-1-recall")
    blinded = blind([series_1], {}, {ref: "Velvet Pika says June."}, NAMES)

    (packet,) = blinded.recall_packets
    assert packet.key == series_1.meetings[-1].recall_key
    assert packet.reply == f"{ADVISER_PLACEHOLDER} says June."
    assert blinded.seal[packet.id] == ref


def test_blind_refuses_a_memo_for_a_meeting_that_is_not_a_plan(series_1: Series) -> None:
    with pytest.raises(ValueError, match="series-1-briefing"):
        blind([series_1], {MemoRef("A", "series-1", "series-1-briefing"): "m"}, {}, NAMES)


def test_each_rater_gets_the_packets_in_a_different_order() -> None:
    packets = list(range(6))
    orders = rater_orders(packets, ("person-1", "person-2", "judge"), rng=random.Random(0))

    assert all(sorted(o) == packets for o in orders.values())
    assert len({tuple(o) for o in orders.values()}) == 3


def test_rater_orders_draws_again_when_two_raters_would_share_an_order() -> None:
    class Stubborn(random.Random):
        """Shuffles the same way twice, then differently."""

        calls = 0

        def shuffle(self, x):  # type: ignore[no-untyped-def,override]
            self.calls += 1
            if self.calls <= 2:
                x.reverse()
            else:
                super().shuffle(x)

    orders = rater_orders([1, 2, 3, 4], ("a", "b"), rng=Stubborn(1))
    assert orders["a"] != orders["b"]


def test_rater_orders_with_too_few_packets_to_differ_still_finishes() -> None:
    orders = rater_orders([1], ("person-1", "person-2", "judge"), rng=random.Random(0))
    assert list(orders.values()) == [[1], [1], [1]]
