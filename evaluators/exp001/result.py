"""The figures part 2 of EXP-001's pre-registration names, from every score.

Once the seal is open, every rater's score is read back to the memo or
recall reply it scores (:mod:`evaluators.exp001.scoring_run`). This module
puts PR 2's arithmetic together as part 2 says:

- each memo's **quality**, the raters' average total, a memo never written
  scoring 0 (§3), and each recall answer right when two raters say so;
- **agreement**: each pair of raters' per-plan rank correlation, their
  average, and the pooled figures (§4);
- the **rule that fires**, or why none does (§5, §6);
- everything **§7** reports without deciding: B against A and C against B,
  each arm's quality, dollars and minutes per plan and the two ratios, each
  criterion, the recall checks and the control plans on their own, the rule
  on the two people's scores alone, every series difference of every
  comparison, and each deciding verdict's range of prices and its verdict
  with bids and summaries repriced at the shipped fast model's list price.

When the judge's cap stopped it before every packet, the judge's scores
cannot be averaged in, and the result is inconclusive (part 2 §4); the
figures are then the two people's alone. Building the result reads no file.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from evaluators.exp001.decision import (
    MIN_SERIES,
    T_975,
    Comparison,
    Decision,
    Outcome,
    compare,
    decide,
    price_range,
)
from evaluators.exp001.materials import MeetingKind, Series
from evaluators.exp001.rating import PEOPLE
from evaluators.exp001.scoring import (
    AGREEMENT_FLOOR,
    CRITERIA,
    MEMORY_CRITERION,
    Agreement,
    MemoRef,
    agreement,
    memo_quality,
    rater_total,
    recall_right,
)

# Every comparison §5 and §7 name: the three checks in order, then B's two.
COMPARISONS = (("C", "A"), ("D", "C"), ("D", "D-prime"), ("B", "A"), ("C", "B"))
# The verdict each check reads: the first asks whether C clearly beats A.
_CLEARLY = frozenset({("C", "A")})

Scores = Mapping[str, Mapping[str, int | None]]  # rater → criterion → score
Marks = Mapping[str, Mapping[str, bool]]  # rater → question → right


@dataclass(frozen=True)
class Ratings:
    """Every rater's scores, read back through the seal to what they score."""

    memos: Mapping[MemoRef, Scores]  # each memo written
    recall: Mapping[MemoRef, Marks]  # each recall reply written
    packets: Mapping[MemoRef, str]  # each written answer's packet ID


def build(
    *,
    series: Sequence[Series],
    arms: Sequence[str],
    ratings: Ratings,
    raters: Sequence[str],
    dollars: Mapping[tuple[str, str], float],
    repriced: Mapping[tuple[str, str], float],
    minutes: Mapping[MemoRef, float | None],
    run_complete: bool,
    on_time: bool | None,
    judge_cap_reached: bool,
) -> dict[str, Any]:
    """The figures of *ratings* over the kept *series*, each memo's quality
    the average of *raters*. *dollars* are each arm's dollars per plan, by
    arm and series, and *repriced* the same with bids and summaries at the
    fast model's price; *minutes* each plan's minutes, None when its memo
    was never written. *on_time* is None for a run with no due date."""
    kinds = {MemoRef(a, s.id, m.id): m.kind for s in series for a in arms for m in s.meetings}
    plans = [ref for ref, kind in kinds.items() if kind.scored]
    quality = {ref: memo_quality(ratings.memos.get(ref), kinds[ref], raters) for ref in plans}
    totals = _totals(ratings, kinds, raters)
    agreed = agreement(totals, raters)
    kept = [s.id for s in series]
    conditions = {"run_complete": run_complete, "scoring_on_time": on_time is not False,
                  "judge_cap_reached": judge_cap_reached}
    decision = decide(quality, dollars, kept, agreement_sufficient=agreed.sufficient,
                      **conditions)
    comparisons = []
    if len(kept) in T_975:
        deciding = {(c.x, c.y): c for c in decision.checks}
        comparisons = [_comparison(quality, dollars, repriced, x, y, kept, deciding.get((x, y)))
                       for x, y in COMPARISONS]
    people_quality = {ref: memo_quality(ratings.memos.get(ref), kinds[ref], PEOPLE)
                      for ref in plans}
    people_agreed = agreement(_totals(ratings, kinds, PEOPLE), PEOPLE)
    people_decision = decide(people_quality, dollars, kept,
                             agreement_sufficient=people_agreed.sufficient,
                             **{**conditions, "judge_cap_reached": False})
    return {
        "raters": list(raters),
        "judge_cap_reached": judge_cap_reached,
        "series": kept,
        "outcome": decision.outcome.value,
        "why": _why(decision, kept, agreed, conditions),
        "agreement": _agreement(agreed),
        "comparisons": comparisons,
        "repriced_outcome": decide(quality, repriced, kept,
                                   agreement_sufficient=agreed.sufficient,
                                   **conditions).outcome.value,
        "people_alone": {
            "outcome": people_decision.outcome.value,
            "why": _why(people_decision, kept, people_agreed,
                        {**conditions, "judge_cap_reached": False}),
            "agreement": people_agreed.mean,
            "checks": [_verdict_of(c) for c in people_decision.checks],
        },
        "arms": {arm: _arm(arm, series, kinds, ratings, raters, quality, dollars, repriced,
                           minutes) for arm in arms},
        "memos": [_memo(ref, kinds[ref], ratings, raters, quality[ref]) for ref in plans],
        "recall": [_recall(ref, ratings, raters, series) for ref, kind in kinds.items()
                   if kind is MeetingKind.RECALL],
    }


def _totals(
    ratings: Ratings, kinds: Mapping[MemoRef, MeetingKind], raters: Sequence[str],
) -> dict[MemoRef, dict[str, float]]:
    """Each written memo's total from each of *raters*, as agreement reads them."""
    return {ref: {r: rater_total(scores[r], kinds[ref]) for r in raters}
            for ref, scores in ratings.memos.items() if ref in kinds}


def _why(
    decision: Decision, kept: Sequence[str], agreed: Agreement, conditions: Mapping[str, bool],
) -> str | None:
    """Why no rule fires, in the order :func:`decide` asks; None when one does."""
    if decision.outcome is Outcome.INCOMPLETE:
        return "fewer than four series" if len(kept) < MIN_SERIES else "the run is incomplete"
    if decision.outcome is not Outcome.INCONCLUSIVE:
        return None
    if not agreed.sufficient:
        return f"the raters' agreement is below {AGREEMENT_FLOOR}"
    if not conditions["scoring_on_time"]:
        return "scoring finished after its 21 days"
    return "the judge's spend reached its cap"


def _agreement(agreed: Agreement) -> dict[str, Any]:
    return {
        "pairs": [{"raters": list(pair), "mean": figure.mean, "plans_used": figure.plans_used,
                   "plans_skipped": figure.plans_skipped, "pooled": agreed.pooled[pair]}
                  for pair, figure in agreed.pairs.items()],
        "mean": agreed.mean,
        "floor": AGREEMENT_FLOOR,
        "sufficient": agreed.sufficient,
    }


def _comparison(
    quality: Mapping[MemoRef, float], dollars: Mapping[tuple[str, str], float],
    repriced: Mapping[tuple[str, str], float], x: str, y: str, kept: Sequence[str],
    deciding: Comparison | None,
) -> dict[str, Any]:
    """One comparison, every series difference shown; a check the decision
    read also carries its verdict's range of prices and its repriced verdict."""
    c = deciding or compare(quality, dollars, x, y, kept)
    again = compare(quality, repriced, x, y, kept)
    out = {**_figures(c), "deciding": deciding is not None, "verdict": None,
           "price_range": None, "repriced": _figures(again)}
    if deciding is not None:
        clearly = (x, y) in _CLEARLY
        holds = c.clearly_beats if clearly else c.beats
        out["verdict"] = {"name": "clearly beats" if clearly else "beats", "holds": holds}
        out["price_range"] = [
            [lo, None if math.isinf(hi) else hi]
            for lo, hi in price_range(quality, dollars, x, y, kept, clearly=clearly, holds=holds)
        ]
    return out


def _figures(c: Comparison) -> dict[str, Any]:
    return {
        "x": c.x, "y": c.y, "differences": list(c.differences),
        "interval": {"mean": c.interval.mean, "low": c.interval.low, "high": c.interval.high},
        "beats": c.beats, "clearly_beats": c.clearly_beats,
    }


def _verdict_of(c: Comparison) -> dict[str, Any]:
    return {"x": c.x, "y": c.y, "beats": c.beats, "clearly_beats": c.clearly_beats}


def _arm(
    arm: str, series: Sequence[Series], kinds: Mapping[MemoRef, MeetingKind], ratings: Ratings,
    raters: Sequence[str], quality: Mapping[MemoRef, float],
    dollars: Mapping[tuple[str, str], float], repriced: Mapping[tuple[str, str], float],
    minutes: Mapping[MemoRef, float | None],
) -> dict[str, Any]:
    """Part 2 §7's figures for one arm over the kept series."""
    mine = [ref for ref in quality if ref.arm == arm]
    q = statistics.fmean(quality[ref] for ref in mine)
    per_plan = statistics.fmean(dollars[(arm, s.id)] for s in series)
    timed = [t for ref in mine if (t := minutes.get(ref)) is not None]
    per_minute = statistics.fmean(timed) if timed else None
    right = asked = 0
    for s in series:
        for meeting in s.meetings:
            if meeting.recall_key is None:
                continue
            marks = ratings.recall.get(MemoRef(arm, s.id, meeting.id))
            asked += len(meeting.recall_key)
            if marks is not None:
                right += sum(_right([marks[r][question] for r in raters])
                             for question in meeting.recall_key)
    return {
        "quality": q,
        "plans_quality": _mean(quality[r] for r in mine if kinds[r] is MeetingKind.PLAN),
        "control_quality": _mean(quality[r] for r in mine if kinds[r] is MeetingKind.CONTROL),
        "criteria": {c: _criterion(c, mine, kinds, ratings, raters) for c in CRITERIA},
        "dollars_per_plan": per_plan,
        "dollars_per_plan_repriced": statistics.fmean(repriced[(arm, s.id)] for s in series),
        "quality_per_dollar": q / per_plan if per_plan else None,
        "minutes_per_plan": per_minute,
        "minutes_left_out": len(mine) - len(timed),
        "quality_per_minute": q / per_minute if per_minute else None,
        "recall": {"right": right, "asked": asked},
    }


def _criterion(
    criterion: str, refs: Sequence[MemoRef], kinds: Mapping[MemoRef, MeetingKind],
    ratings: Ratings, raters: Sequence[str],
) -> float | None:
    """One criterion's average score, every rater and memo counted, a memo
    never written scoring 0; C2 over the plans that score it."""
    scores: list[float] = []
    for ref in refs:
        if criterion == MEMORY_CRITERION and kinds[ref] is MeetingKind.CONTROL:
            continue
        given = ratings.memos.get(ref)
        scores += [0.0] * len(raters) if given is None else [
            float(given[r][criterion] or 0) for r in raters
        ]
    return _mean(scores)


def _memo(
    ref: MemoRef, kind: MeetingKind, ratings: Ratings, raters: Sequence[str], quality: float,
) -> dict[str, Any]:
    given = ratings.memos.get(ref)
    return {
        **ref._asdict(), "kind": kind.value, "packet": ratings.packets.get(ref),
        "missing": given is None,
        "totals": None if given is None else {r: rater_total(given[r], kind) for r in raters},
        "quality": quality,
    }


def _recall(
    ref: MemoRef, ratings: Ratings, raters: Sequence[str], series: Sequence[Series],
) -> dict[str, Any]:
    marks = ratings.recall.get(ref)
    meeting = next(m for s in series if s.id == ref.series for m in s.meetings
                   if m.id == ref.meeting)
    assert meeting.recall_key is not None  # a recall meeting has its key
    return {
        **ref._asdict(), "packet": ratings.packets.get(ref), "missing": marks is None,
        "questions": {
            q: {"marks": None if marks is None else {r: marks[r][q] for r in raters},
                "right": marks is not None and _right([marks[r][q] for r in raters])}
            for q in meeting.recall_key
        },
    }


def _right(marks: Sequence[bool]) -> bool:
    """Right when two raters say so: of three, or of the two people alone."""
    return recall_right(marks) if len(marks) == 3 else sum(marks) >= 2


def _mean(values: Any) -> float | None:
    values = list(values)
    return statistics.fmean(values) if values else None
