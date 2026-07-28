"""RFC 0049 PR 4 — the shadow→live promotion measurement.

The RFC 0049 Phase-1 widenings (L2 cross-room facts, L1 room-first-ranked
episodes) shipped shadow-first: PRs 2–3 record what the widened recall
*would* have injected as ``tier``-keyed traces the RFC 0044 harness
threads into the report artifact (``shadow_traces``). This module is the
measurement consumer those PRs deferred to: it partitions a run's traces
by tier, summarizes each tier's would-inject volume and gate outcomes,
and renders the promotion verdict the shadow→live flip was gated on.

The verdict criteria (each mapped to a named boolean in
:class:`PromotionVerdict.criteria`):

* ``label_integrity`` — zero ``unknown_label`` withholds across every
  trace. The trace splits withhold counts by cause exactly so this
  criterion can tell "gate working" (clean above-rank ``withheld``, which
  does NOT fail promotion — it is the gate doing its job) from "labels
  corrupt" (rule (c): a stored protection label failed to parse), which
  does.
* ``bounded_volume`` — no single turn's gate-admitted delta exceeds the
  per-tier bound (defaults mirror the runtime recall limits:
  ``episodic`` 5 = ``EPISODIC_RECALL_LIMIT`` — a bound the widened read
  holds by construction; ``facts`` 20 = ``FACTS_RECALL_LIMIT``, one
  recall's width — a turn spanning several seeds may legitimately carry
  more rows than one seed returns, but exceeding a full recall's width
  signals seed-flooding). This is the RFC 0017 flood criterion at the
  row level; token-level admission stays with ``MemoryBudget``, which
  the live path funnels every candidate through anyway.
* ``continuity`` — the caller-supplied golden-replay outcome: the landed
  single-room goldens (the dementia bar, EVAL-MEMORY-001) plus the
  cross-room seeds replay green. Passed in rather than recomputed —
  replaying goldens needs the runner + the agents runtime, and this
  module stays pure (importable from ``import evaluators``-light
  contexts, the assertion-core contract).

Reading the traces honestly (the #783 review carry-notes):

* Shadow ranks are **marginally pessimistic**: the shadow pass runs
  after the live recall's reinforcement bump, so a live row's
  ``access_count`` is one higher than the widened row competed against.
  Noted in every verdict (:data:`NOTE_RANK_PESSIMISM`) — do not chase
  sub-position rank discrepancies.
* ``acting: null`` in a trace means the turn's acting classification was
  **unstamped and floored to the rule-(b) public level** — the gate ran
  at ``public``; it was not skipped. Summarized as ``acting_floored``,
  never treated as missing data.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_TIER_BOUNDS",
    "NOTE_ACTING_FLOOR",
    "NOTE_RANK_PESSIMISM",
    "PromotionVerdict",
    "TierSummary",
    "partition_traces",
    "promotion_verdict",
    "summarize_tier",
]

#: Per-tier per-turn admitted-candidate bounds for ``bounded_volume``
#: (see the module docstring for why these two numbers).  Callers with
#: the runtime importable should pass the live constants instead; the
#: verdict test pins these defaults against them so they cannot drift.
DEFAULT_TIER_BOUNDS: dict[str, int] = {"episodic": 5, "facts": 20}

NOTE_RANK_PESSIMISM = (
    "shadow ranks are marginally pessimistic: the shadow pass runs after "
    "live reinforcement, so live rows carry one extra access_count bump "
    "(#783 note 1) — do not chase sub-position rank discrepancies"
)
NOTE_ACTING_FLOOR = (
    "acting=null traces are unstamped turns floored to the rule-(b) "
    "public acting level (#783 note 4) — the gate ran at public, it was "
    "not skipped"
)


@dataclass(frozen=True)
class TierSummary:
    """One tier's aggregate view of a run's shadow traces."""

    tier: str
    trace_count: int
    candidate_count: int
    withheld: int
    unknown_label: int
    acting_floored: int
    max_candidates_per_turn: int
    #: Episodic only — the deepest ``rank`` (0-based widened-result
    #: position) any admitted candidate carried; ``None`` when no
    #: candidate carried a rank (facts traces never do).
    max_rank: int | None


@dataclass(frozen=True)
class PromotionVerdict:
    """The shadow→live verdict: green iff every criterion holds."""

    criteria: dict[str, bool]
    tiers: list[TierSummary] = field(default_factory=list)
    notes: tuple[str, ...] = (NOTE_RANK_PESSIMISM, NOTE_ACTING_FLOOR)

    @property
    def green(self) -> bool:
        return all(self.criteria.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "green": self.green,
            "criteria": dict(self.criteria),
            "tiers": [vars(t) for t in self.tiers],
            "notes": list(self.notes),
        }


def partition_traces(
    traces: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Partition a merged shadow-trace stream by its ``tier`` key.

    The harness records one chronologically-merged stream across both
    shadow loggers; ``tier`` ("facts" / "episodic") is the discriminator
    the PR 3 trace-shape change added for exactly this consumer. A trace
    missing the key lands under ``"unknown"`` — surfaced rather than
    dropped, so a future third tier (or a shape regression) is visible
    in the summary instead of silently uncounted.
    """
    out: dict[str, list[dict[str, Any]]] = {}
    for trace in traces:
        tier = trace.get("tier")
        key = tier if isinstance(tier, str) and tier else "unknown"
        out.setdefault(key, []).append(trace)
    return out


def summarize_tier(tier: str, traces: list[dict[str, Any]]) -> TierSummary:
    """Aggregate one tier's traces (both withhold fields read — the 0031
    amendment trace-shape contract)."""
    candidate_count = 0
    withheld = 0
    unknown_label = 0
    acting_floored = 0
    max_per_turn = 0
    max_rank: int | None = None
    for trace in traces:
        candidates = trace.get("candidates") or []
        candidate_count += len(candidates)
        max_per_turn = max(max_per_turn, len(candidates))
        withheld += int(trace.get("withheld") or 0)
        unknown_label += int(trace.get("unknown_label") or 0)
        if trace.get("acting") is None:
            acting_floored += 1
        for cand in candidates:
            rank = cand.get("rank")
            if isinstance(rank, int) and (max_rank is None or rank > max_rank):
                max_rank = rank
    return TierSummary(
        tier=tier,
        trace_count=len(traces),
        candidate_count=candidate_count,
        withheld=withheld,
        unknown_label=unknown_label,
        acting_floored=acting_floored,
        max_candidates_per_turn=max_per_turn,
        max_rank=max_rank,
    )


def promotion_verdict(
    traces: list[dict[str, Any]],
    *,
    goldens_green: bool,
    tier_bounds: dict[str, int] | None = None,
) -> PromotionVerdict:
    """Render the shadow→live promotion verdict for a run's traces.

    ``goldens_green`` is the replay outcome of the full eval suite (the
    continuity criterion — see the module docstring). ``tier_bounds``
    overrides :data:`DEFAULT_TIER_BOUNDS`; a tier with traces but no
    bound fails ``bounded_volume`` (an unbounded tier is a flood by
    definition, and the honest default for a tier this module has never
    heard of).
    """
    bounds = DEFAULT_TIER_BOUNDS if tier_bounds is None else tier_bounds
    summaries = [
        summarize_tier(tier, tier_traces)
        for tier, tier_traces in sorted(partition_traces(traces).items())
    ]
    bounded = all(
        s.max_candidates_per_turn <= bounds[s.tier]
        for s in summaries
        if s.tier in bounds
    ) and all(s.tier in bounds for s in summaries)
    return PromotionVerdict(
        criteria={
            "label_integrity": sum(s.unknown_label for s in summaries) == 0,
            "bounded_volume": bounded,
            "continuity": goldens_green,
        },
        tiers=summaries,
    )


# ─── CLI (`python -m evaluators.shadow_measurement <report.json>`) ────────────


def _traces_from_report(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect every eval's ``shadow_traces`` from a suite report artifact
    (the ``make eval-replay REPORT=<path>`` output — the key is present
    only on evals whose run produced traces)."""
    out: list[dict[str, Any]] = []
    for entry in report.get("evals") or []:
        out.extend(entry.get("shadow_traces") or [])
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="evaluators.shadow_measurement",
        description=(
            "RFC 0049 PR 4 shadow→live promotion verdict over a suite "
            "report artifact (make eval-replay REPORT=<path>)."
        ),
    )
    parser.add_argument("report", help="suite report JSON written by the runner")
    args = parser.parse_args(argv)
    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    verdict = promotion_verdict(
        _traces_from_report(report),
        goldens_green=bool((report.get("summary") or {}).get("passed_all")),
    )
    print(json.dumps(verdict.to_dict(), indent=2))
    return 0 if verdict.green else 1


if __name__ == "__main__":  # pragma: no cover — exercised via the CLI tests
    sys.exit(main())
