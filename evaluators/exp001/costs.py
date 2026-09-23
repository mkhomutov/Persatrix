"""What each EXP-001 model call cost, priced with the pre-registered table.

The run records every model call it makes, whichever arm makes it (check 4).
Dollars come from the fixed table below, never from the orchestrator's own
ledger, which has no price for cached tokens and misses some memory
summaries. Two totals matter:

- an arm's **dollars per plan**: what it spent on one series' briefing and
  four plans, in the attempt that finished, divided by four;
- **real spend**: every call made, counted or not, which the $150 cap reads.

This module also draws the order the five arms run in within each series.
"""

from __future__ import annotations

import datetime as dt
import enum
import random
from collections.abc import Iterable
from dataclasses import dataclass

from evaluators.exp001.materials import PLANS_PER_SCORED_SERIES, MeetingKind

ARMS = ("A", "B", "C", "D", "D-prime")
ORDER_SEED = 2026
_PER_MILLION = 1_000_000


@dataclass(frozen=True)
class Price:
    """US dollars per million tokens. ``None`` means the model has no cache price."""

    input: float
    output: float
    cache_write: float | None = None
    cache_read: float | None = None


PRICES: dict[str, Price] = {
    "claude-sonnet-4-6": Price(input=3.00, output=15.00, cache_write=3.75, cache_read=0.30),
    "claude-opus-5": Price(input=5.00, output=25.00),
}


class CallPurpose(enum.Enum):
    REPLY = "reply"  # an adviser's turn in the discussion, or arm A's one call
    BID = "bid"  # a salience bid
    MEMO = "memo"  # the chair's memo turn
    SUMMARY = "summary"  # a memory summary or fact extraction
    JUDGE = "judge"  # the LLM judge scoring a memo or recall check


@dataclass(frozen=True)
class CallRecord:
    """One model call, with everything check 4 of the pre-registration asks for."""

    arm: str
    series: str
    meeting: str
    meeting_kind: MeetingKind
    attempt: int
    adviser: str | None  # None for arm A's single call and for the judge
    purpose: CallPurpose
    started_at: dt.datetime
    model: str
    input_tokens: int
    output_tokens: int
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    # False for memory writes in an arm whose design has no memory (B, C,
    # D-prime) when the runtime cannot switch them off.
    counts_in_arm: bool = True


def price_call(call: CallRecord) -> float:
    price = PRICES[call.model]
    dollars = call.input_tokens * price.input + call.output_tokens * price.output
    for tokens, rate in (
        (call.cache_write_tokens, price.cache_write),
        (call.cache_read_tokens, price.cache_read),
    ):
        if tokens and rate is None:
            raise ValueError(f"{call.model} has no cache price, but the call reports cache tokens")
        dollars += tokens * (rate or 0.0)
    return dollars / _PER_MILLION


def dollars_per_plan(
    records: Iterable[CallRecord], *, arm: str, series: str, attempt: int
) -> float:
    """The arm's spend on the series' briefing and plans in one attempt, per plan."""
    total = sum(
        price_call(r)
        for r in records
        if r.arm == arm
        and r.series == series
        and r.attempt == attempt
        and r.counts_in_arm
        and r.meeting_kind is not MeetingKind.RECALL
    )
    return total / PLANS_PER_SCORED_SERIES


def real_spend(records: Iterable[CallRecord]) -> float:
    return sum(price_call(r) for r in records)


def arm_orders(n_series: int, seed: int = ORDER_SEED) -> list[tuple[str, ...]]:
    """Each series' arm order, series 1 first, all drawn from one stream."""
    stream = random.Random(seed)
    return [tuple(stream.sample(ARMS, len(ARMS))) for _ in range(n_series)]
