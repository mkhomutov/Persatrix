"""What each EXP-001 model call cost, priced with the pre-registered table.

The run records every model call it makes, whichever arm makes it (check 4).
Dollars come from the fixed table below, never from the orchestrator's own
ledger, which has no price for cached tokens and misses some memory
summaries. Two totals matter:

- an arm's **dollars per plan**: what it spent on one series' briefing and
  four plans, in the attempt that finished, divided by four;
- **real spend**: every call the arms made in the scored run, counted or not,
  which the $150 cap reads. Practice calls and the judge are left out; the
  judge has its own $25 cap, read from **judging spend**.

This module also draws the order the five arms run in within each series.
"""

from __future__ import annotations

import datetime as dt
import enum
import random
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace

from evaluators.exp001.materials import PLANS_PER_SCORED_SERIES, PRACTICE_SERIES, MeetingKind

ARMS = ("A", "B", "C", "D", "D-prime")
# The one model every arm calls, for every purpose (pre-registration §2).
ARMS_MODEL = "claude-sonnet-4-6"
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
    ARMS_MODEL: Price(input=3.00, output=15.00, cache_write=3.75, cache_read=0.30),
    "claude-opus-5": Price(input=5.00, output=25.00),
}

# Part 2 §7 reports each deciding verdict again with bids and memory
# summaries repriced at the shipped fast model's list price. Its cache prices
# keep the fixed table's ratio to input: 1.25x to write, 0.1x to read. It is
# kept out of PRICES, so no call the run makes is ever priced at it.
REPRICE_MODEL = "claude-haiku-4-5"
REPRICING_PRICES: dict[str, Price] = {
    **PRICES,
    REPRICE_MODEL: Price(input=1.00, output=5.00, cache_write=1.25, cache_read=0.10),
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


def price_call(call: CallRecord, prices: Mapping[str, Price] = PRICES) -> float:
    price = prices[call.model]
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
    records: Iterable[CallRecord],
    *,
    arm: str,
    series: str,
    attempt: int,
    prices: Mapping[str, Price] = PRICES,
) -> float:
    """The arm's spend on the series' briefing and plans in one attempt, per plan."""
    total = sum(
        price_call(r, prices)
        for r in records
        if r.arm == arm
        and r.series == series
        and r.attempt == attempt
        and r.counts_in_arm
        and r.purpose is not CallPurpose.JUDGE
        and r.meeting_kind is not MeetingKind.RECALL
    )
    return total / PLANS_PER_SCORED_SERIES


def repriced(records: Iterable[CallRecord]) -> list[CallRecord]:
    """The records with every bid and summary moved to the fast model, for §7's report."""
    cheap = (CallPurpose.BID, CallPurpose.SUMMARY)
    return [replace(r, model=REPRICE_MODEL) if r.purpose in cheap else r for r in records]


def real_spend(records: Iterable[CallRecord]) -> float:
    """Every arm call in the scored series, any attempt, counted or not."""
    return sum(
        price_call(r)
        for r in records
        if r.purpose is not CallPurpose.JUDGE and r.series != PRACTICE_SERIES
    )


def judging_spend(records: Iterable[CallRecord]) -> float:
    return sum(price_call(r) for r in records if r.purpose is CallPurpose.JUDGE)


def arm_orders(n_series: int, seed: int = ORDER_SEED) -> list[tuple[str, ...]]:
    """Each series' arm order, series 1 first, all drawn from one stream."""
    stream = random.Random(seed)
    return [tuple(stream.sample(ARMS, len(ARMS))) for _ in range(n_series)]
