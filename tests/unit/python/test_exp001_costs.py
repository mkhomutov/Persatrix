"""EXP-001 harness — call records, the fixed price table, and the arm order.

Pre-registration §3 fixes the prices per million tokens, what an arm's
"dollars per plan" counts (the briefing and the four plans of the attempt
that finished, not the recall check, not a failed attempt), and that the
$150 cap counts every call made. It also fixes that each series draws its
arm order from one random stream seeded 2026.
"""

from __future__ import annotations

import datetime as dt

import pytest

from evaluators.exp001.costs import (
    ARMS,
    PRICES,
    CallPurpose,
    CallRecord,
    arm_orders,
    dollars_per_plan,
    judging_spend,
    price_call,
    real_spend,
)
from evaluators.exp001.materials import MeetingKind

_T0 = dt.datetime(2026, 10, 1, 9, 0, tzinfo=dt.UTC)


def _call(
    *,
    arm: str = "C",
    series: str = "series-1",
    meeting_kind: MeetingKind = MeetingKind.PLAN,
    attempt: int = 1,
    purpose: CallPurpose = CallPurpose.REPLY,
    input_tokens: int = 1_000_000,
    output_tokens: int = 0,
    cache_write: int = 0,
    cache_read: int = 0,
    counts_in_arm: bool = True,
    model: str = "claude-sonnet-4-6",
) -> CallRecord:
    return CallRecord(
        arm=arm,
        series=series,
        meeting=f"{series}-x",
        meeting_kind=meeting_kind,
        attempt=attempt,
        adviser="velvet-pika",
        purpose=purpose,
        started_at=_T0,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_write_tokens=cache_write,
        cache_read_tokens=cache_read,
        counts_in_arm=counts_in_arm,
    )


def test_price_table_is_the_preregistered_one() -> None:
    sonnet = PRICES["claude-sonnet-4-6"]
    assert (sonnet.input, sonnet.output, sonnet.cache_write, sonnet.cache_read) == (
        3.00,
        15.00,
        3.75,
        0.30,
    )
    opus = PRICES["claude-opus-5"]
    assert (opus.input, opus.output) == (5.00, 25.00)


def test_price_call_charges_each_token_kind_at_its_rate() -> None:
    call = _call(
        input_tokens=200_000, output_tokens=10_000, cache_write=400_000, cache_read=1_000_000
    )
    # 0.2*3 + 0.01*15 + 0.4*3.75 + 1.0*0.30
    assert price_call(call) == pytest.approx(0.6 + 0.15 + 1.5 + 0.3)


def test_price_call_refuses_an_unpriced_model() -> None:
    with pytest.raises(KeyError, match="claude-haiku"):
        price_call(_call(model="claude-haiku-4-5"))


def test_price_call_refuses_cache_tokens_on_a_model_without_cache_prices() -> None:
    with pytest.raises(ValueError, match="cache"):
        price_call(_call(model="claude-opus-5", cache_read=10))


def test_dollars_per_plan_counts_briefing_and_plans_of_the_given_attempt_only() -> None:
    records = [
        _call(meeting_kind=MeetingKind.BRIEFING),  # $3
        _call(meeting_kind=MeetingKind.PLAN),  # $3
        _call(meeting_kind=MeetingKind.CONTROL),  # $3
        _call(meeting_kind=MeetingKind.RECALL),  # not counted: recall check
        _call(attempt=2),  # not counted: another attempt
        _call(arm="D"),  # not counted: another arm
        _call(series="series-2"),  # not counted: another series
        _call(counts_in_arm=False),  # not counted: memory write outside the arm's design
        _call(purpose=CallPurpose.JUDGE, model="claude-opus-5"),  # not counted: the judge
    ]
    assert dollars_per_plan(records, arm="C", series="series-1", attempt=1) == pytest.approx(
        9.0 / 4
    )


def test_real_spend_counts_every_call_made() -> None:
    records = [
        _call(meeting_kind=MeetingKind.RECALL),
        _call(attempt=2),
        _call(counts_in_arm=False),
        _call(arm="A"),
        _call(series="practice"),  # not counted: practice is not the scored run
        _call(purpose=CallPurpose.JUDGE, model="claude-opus-5"),  # judging has its own cap
    ]
    assert real_spend(records) == pytest.approx(12.0)


def test_judging_spend_counts_only_judge_calls() -> None:
    records = [
        _call(),
        _call(purpose=CallPurpose.JUDGE, model="claude-opus-5"),  # $5
    ]
    assert judging_spend(records) == pytest.approx(5.0)


def test_arm_orders_are_drawn_from_one_stream_seeded_2026() -> None:
    orders = arm_orders(5)

    assert len(orders) == 5
    assert all(sorted(o) == sorted(ARMS) for o in orders)
    assert orders == arm_orders(5)  # reproducible
    assert len(set(orders)) > 1  # series do not share one order
    # One stream: series 1's order is the first draw, whatever n is.
    assert arm_orders(1)[0] == orders[0]


def test_arm_orders_are_the_recorded_ones() -> None:
    """The orders docs/experiments/EXP-001-harness.md records; a change here is a harness fault."""
    assert arm_orders(5) == [
        ("A", "C", "D", "D-prime", "B"),
        ("D-prime", "D", "C", "B", "A"),
        ("B", "A", "C", "D", "D-prime"),
        ("C", "A", "B", "D", "D-prime"),
        ("C", "B", "D", "D-prime", "A"),
    ]


def test_arms_are_the_five_preregistered_arms() -> None:
    assert ARMS == ("A", "B", "C", "D", "D-prime")
