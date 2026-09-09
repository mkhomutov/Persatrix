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
* ``audience_delta_measured`` — the fourth criterion, added by v0.3.16
  PR A2 for ISSUE-0132 and rendered only when the caller asks
  (``audience_expected=True`` / ``--audience``). The first three can
  only go red on a *defect*; this one exists because in shadow nothing
  else moves, so a verdict that only checked for defects would go green
  on a run where no disjoint audience ever occurred — the flip would
  then ship on a vacuous measurement. What it asserts is therefore that
  the delta was **measured over a sample that exercised the case**: a
  disjoint AND an admit verdict, over at least
  :data:`AUDIENCE_MIN_JUDGED_TIERS` of the three judged tiers. The
  delta's own *threshold* is PR A3's argument, stated in that PR, not
  hard-coded here. The number itself
  (:class:`AudienceSummary.withhold_share`, reported per cause and per
  acting-room shape) rides the verdict either way.

Why this module renders three shadows and not two: the audience check is
a different *kind* of widening from the RFC 0049 pair — those widen what
is recalled, it narrows what may be spoken — but the promotion pattern
is identical (shadow → verdict → flip), so it reuses the machinery
rather than growing a second one.

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
* A zero ``withhold-unknown-fetch-failed`` count is **not evidence**
  offline: the eval driver's roster seam is an in-process map that
  cannot fail, so that cause is only observable on the live MT leg
  (:data:`NOTE_FETCH_FAILED_IS_LIVE_ONLY`, v0.3.16 scope lock 1).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "AUDIENCE_MIN_JUDGED_TIERS",
    "AUDIENCE_TIER",
    "AUDIENCE_TURN_BOUND",
    "DEFAULT_TIER_BOUNDS",
    "NOTE_ACTING_FLOOR",
    "NOTE_FETCH_FAILED_IS_LIVE_ONLY",
    "NOTE_RANK_PESSIMISM",
    "AudienceSummary",
    "PromotionVerdict",
    "TierSummary",
    "partition_traces",
    "promotion_verdict",
    "room_shape",
    "summarize_audience",
    "summarize_tier",
]

#: The ISSUE-0132 partition (v0.3.16 A2) — mirrors
#: ``agents.persona_runtime.audience_shadow.AUDIENCE_TIER``.
AUDIENCE_TIER: str = "audience"

#: Per-tier per-turn admitted-candidate bounds for ``bounded_volume``
#: (see the module docstring for why these two numbers).  Callers with
#: the runtime importable should pass the live constants instead; the
#: verdict test pins these defaults against them so they cannot drift.
#: ``audience`` is deliberately NOT a member: it is not a recall tier
#: and does not go through ``summarize_tier`` at all (see
#: :func:`promotion_verdict`), so adding it here would make every caller
#: that passes its own ``tier_bounds`` fail ``bounded_volume`` the day an
#: audience trace appears in its report — a red verdict naming a
#: criterion with nothing to do with the cause.
DEFAULT_TIER_BOUNDS: dict[str, int] = {"episodic": 5, "facts": 20}

#: The audience check's own per-turn ceiling: the THREE judged tiers'
#: recall limits summed (channel_history 20 + facts 20 + episodic 5).
#: ``notes`` is excluded because ``AUDIENCE_TIERS`` excludes it — a note
#: carries no provenance to judge — so counting its limit here would
#: leave the bound five entries of slack it was never meant to have.
AUDIENCE_TURN_BOUND: int = 45

#: How many of the three judged tiers a sample must actually exercise
#: before ``audience_delta_measured`` will call the delta measured.  Two,
#: not three: ``channel_history`` and ``episodic`` both need a closed
#: interaction to produce a cross-room candidate, so demanding all three
#: from one recipe would gate the flip on a seed shape rather than on
#: evidence — but one tier is a sample, not a measurement.
AUDIENCE_MIN_JUDGED_TIERS: int = 2

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
NOTE_FETCH_FAILED_IS_LIVE_ONLY = (
    "withhold-unknown-fetch-failed cannot occur offline: the eval "
    "driver's in-process roster seam never fails, so a zero here is the "
    "harness, not evidence — that count is an MT Leg 5 LIVE criterion "
    "(v0.3.16 scope lock 1)"
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


def room_shape(channel_id: str | None) -> str:
    """The acting room's *shape* — the axis lock 1 asks the delta be
    reported along, because a DM and a standup fail differently: a DM
    with the wrong person is a two-body problem, while a default group
    room holds three personas none of which was in the teaching DM.

    The prefix vocabulary is ``internal/channels/identifiers.go``'s
    (``dm:`` / ``group:`` / ``thread:``); anything else lands under
    ``other`` rather than being dropped.
    """
    if not channel_id:
        return "unknown"
    for prefix in ("dm:", "group:", "thread:"):
        if channel_id.startswith(prefix):
            return prefix.rstrip(":")
    return "other"


@dataclass(frozen=True)
class AudienceSummary:
    """The ISSUE-0132 delta: what the audience check would have withheld.

    ``judged`` is the denominator lock 1 names — §D-*admitted*,
    ``internal``-and-above entries, the only ones that carry a verdict
    (``public`` is exempt and a classification withhold never reaches
    the check).  ``withhold_share`` over that denominator is the number
    PR A3 must argue against a stated threshold, because in shadow
    nothing else moves.
    """

    trace_count: int
    judged: int
    verdicts: dict[str, int]
    #: disjoint / judged — the delta itself.
    withhold_share: float
    #: (fetch-failed + no-provenance) / judged.
    unknown_share: float
    #: Acting-room shape → that shape's verdict counts.
    by_room_shape: dict[str, dict[str, int]]
    #: Judged tier → that tier's verdict counts.  Reported because the
    #: check spans three tiers and a sample that exercises one of them
    #: measures one of them, however healthy the headline share looks.
    by_tier: dict[str, dict[str, int]]
    #: The most entries any single turn judged — the volume bound, which
    #: rides here rather than in ``DEFAULT_TIER_BOUNDS``.
    max_judged_per_turn: int
    #: Roster round trips vs distinct source rooms — the scope-lock-2
    #: cost bound, reported rather than asserted (a run whose fetches
    #: exceed its source rooms has lost the per-turn cache).
    fetches: int
    source_rooms: int
    #: What ``live`` actually withheld; 0 for a shadow run by definition.
    withheld: int


def summarize_audience(traces: list[dict[str, Any]]) -> AudienceSummary:
    """Aggregate the audience traces of one run."""
    verdicts: dict[str, int] = {}
    by_shape: dict[str, dict[str, int]] = {}
    by_tier: dict[str, dict[str, int]] = {}
    fetches = 0
    source_rooms = 0
    withheld = 0
    max_judged = 0
    for trace in traces:
        shape = room_shape(trace.get("acting_channel_id"))
        shape_counts = by_shape.setdefault(shape, {})
        for verdict, count in (trace.get("verdicts") or {}).items():
            # Every bucket is zero-filled, here and per shape and per
            # tier: a consumer that reads ``by_room_shape["dm"][v]`` must
            # not ``KeyError`` on exactly the runs where that shape scored
            # zero — "measured none" and "not measured" are different
            # facts and the top-level ``verdicts`` already says so.
            verdicts[verdict] = verdicts.get(verdict, 0) + int(count)
            shape_counts[verdict] = shape_counts.get(verdict, 0) + int(count)
        for tier, tier_counts in (trace.get("by_tier") or {}).items():
            bucket = by_tier.setdefault(tier, {})
            for verdict, count in tier_counts.items():
                bucket[verdict] = bucket.get(verdict, 0) + int(count)
        fetches += int(trace.get("fetches") or 0)
        source_rooms += int(trace.get("source_rooms") or 0)
        withheld += int(trace.get("withheld") or 0)
        max_judged = max(max_judged, int(trace.get("judged") or 0))
    judged = sum(verdicts.values())
    disjoint = verdicts.get("withhold-disjoint", 0)
    unknown = (
        verdicts.get("withhold-unknown-fetch-failed", 0)
        + verdicts.get("withhold-unknown-no-provenance", 0)
    )
    return AudienceSummary(
        trace_count=len(traces),
        judged=judged,
        verdicts=verdicts,
        withhold_share=(disjoint / judged) if judged else 0.0,
        unknown_share=(unknown / judged) if judged else 0.0,
        by_room_shape=by_shape,
        by_tier=by_tier,
        max_judged_per_turn=max_judged,
        fetches=fetches,
        source_rooms=source_rooms,
        withheld=withheld,
    )


@dataclass(frozen=True)
class PromotionVerdict:
    """The shadow→live verdict: green iff every criterion holds."""

    criteria: dict[str, bool]
    tiers: list[TierSummary] = field(default_factory=list)
    notes: tuple[str, ...] = (NOTE_RANK_PESSIMISM, NOTE_ACTING_FLOOR)
    #: Present when the caller asked for the ISSUE-0132 criterion.
    audience: AudienceSummary | None = None

    @property
    def green(self) -> bool:
        return all(self.criteria.values())

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "green": self.green,
            "criteria": dict(self.criteria),
            "tiers": [vars(t) for t in self.tiers],
            "notes": list(self.notes),
        }
        if self.audience is not None:
            out["audience"] = vars(self.audience)
        return out


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
    audience_expected: bool = False,
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
    partitioned = partition_traces(traces)
    # The audience partition leaves the RECALL-tier walk before it starts.
    # It is not a recall tier: it has no rank, no rule-(c) casualties, and
    # a ``withheld`` that counts a different thing from every other row's.
    # Forcing it through ``summarize_tier`` bought shape-compatibility and
    # cost every caller with its own ``tier_bounds`` a red ``bounded_volume``
    # the moment an audience trace reached its report.
    audience_traces = partitioned.pop(AUDIENCE_TIER, [])
    summaries = [
        summarize_tier(tier, tier_traces)
        for tier, tier_traces in sorted(partitioned.items())
    ]
    bounded = all(
        s.max_candidates_per_turn <= bounds[s.tier]
        for s in summaries
        if s.tier in bounds
    ) and all(s.tier in bounds for s in summaries)
    criteria = {
        "label_integrity": sum(s.unknown_label for s in summaries) == 0,
        "bounded_volume": bounded,
        "continuity": goldens_green,
    }
    audience: AudienceSummary | None = None
    notes: tuple[str, ...] = (NOTE_RANK_PESSIMISM, NOTE_ACTING_FLOOR)
    if audience_expected:
        audience = summarize_audience(audience_traces)
        # Lock 1's fourth criterion is *not* a threshold — that is PR A3's
        # argument.  What it asserts here is that the delta was measured
        # over a sample that actually exercised the case: the whole risk
        # this shadow guards against is a green verdict on data where no
        # disjoint audience ever occurred.  A run that judged nothing, or
        # judged only same-room entries, is vacuous, not passing.
        #
        # Four clauses, because "exercised the case" has four ways to be
        # false and only the first was being caught:
        #   * nothing judged at all;
        #   * no ``withhold-disjoint`` — the case itself never occurred;
        #   * no ``admit`` — a check that withheld EVERYTHING would pass
        #     the clause above while measuring a broken predicate;
        #   * one tier judged — the check spans three, and a sample that
        #     touches one lets a regression in either other tier ship
        #     green.  ``by_tier`` reports which, so a red here names the
        #     seed to widen rather than the knob to turn.
        criteria["audience_delta_measured"] = (
            audience.judged > 0
            and audience.verdicts.get("withhold-disjoint", 0) > 0
            and audience.verdicts.get("admit", 0) > 0
            and len(audience.by_tier) >= AUDIENCE_MIN_JUDGED_TIERS
        )
        # The volume bound the audience partition kept when it left the
        # tier walk — reported as its own criterion rather than folded
        # into ``bounded_volume``, whose name is about recall.
        criteria["audience_bounded_volume"] = (
            audience.max_judged_per_turn <= AUDIENCE_TURN_BOUND
        )
        notes = (*notes, NOTE_FETCH_FAILED_IS_LIVE_ONLY)
    return PromotionVerdict(
        criteria=criteria, tiers=summaries, notes=notes, audience=audience,
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
    parser.add_argument(
        "--audience", action="store_true",
        help=(
            "render the ISSUE-0132 audience criterion too (v0.3.16). "
            "Explicit rather than inferred from the traces on purpose: "
            "'no audience traces' must read as a red verdict, not as a "
            "run with nothing to say."
        ),
    )
    args = parser.parse_args(argv)
    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    verdict = promotion_verdict(
        _traces_from_report(report),
        goldens_green=bool((report.get("summary") or {}).get("passed_all")),
        audience_expected=bool(args.audience),
    )
    print(json.dumps(verdict.to_dict(), indent=2))
    return 0 if verdict.green else 1


if __name__ == "__main__":  # pragma: no cover — exercised via the CLI tests
    sys.exit(main())
