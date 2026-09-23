"""Which of strategy §4's decision rules EXP-001's scores fire.

Part 2 §5 of the pre-registration prices quality instead of dividing by
dollars: at p = $1 a quality point, a memo's **value** is p × quality minus
its arm's dollars per plan. Two arms are compared series by series, since a
series' four plans share one run of each arm, and the average of those
series differences gets a t-interval. X **beats** Y when the whole interval
is above zero, and **clearly beats** Y when it also averages at least p.

The checks run in order and the first that applies fires its rule. An
incomplete run, or scores that cannot carry a decision, fire none (§6).
"""

from __future__ import annotations

import enum
import math
import statistics
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from evaluators.exp001.materials import PLANS_PER_SCORED_SERIES
from evaluators.exp001.scoring import MemoRef

PRICE_PER_POINT = 1.00
# The 97.5th percentile of Student's t, keyed by the number of series
# compared (n − 1 degrees of freedom). Fewer than four series is incomplete.
T_975 = {5: 2.776, 4: 3.182}
MIN_SERIES = 4
# Verdicts compare figures rounded to this many decimals, so a difference of
# exactly p, or an interval ending exactly at zero, is not decided by how
# floating point happened to round it.
_VERDICT_DIGITS = 9


class Outcome(enum.Enum):
    RULE_1 = "rule 1"  # stop adding society features for this job
    RULE_2 = "rule 2"  # the allocator is not the asset
    RULE_3 = "rule 3"  # long context replaces the allocator
    RULE_4 = "rule 4"  # the full stack is justified
    INCOMPLETE = "incomplete"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True)
class Interval:
    mean: float
    low: float
    high: float


@dataclass(frozen=True)
class Comparison:
    x: str
    y: str
    p: float
    differences: tuple[float, ...]  # one per series, in the order given
    interval: Interval

    @property
    def beats(self) -> bool:
        return round(self.interval.low, _VERDICT_DIGITS) > 0

    @property
    def clearly_beats(self) -> bool:
        return self.beats and round(self.interval.mean - self.p, _VERDICT_DIGITS) >= 0


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    checks: tuple[Comparison, ...]  # the checks read, in order; empty when no rule fires


def series_differences(
    quality: Mapping[MemoRef, float],
    dollars: Mapping[tuple[str, str], float],
    x: str,
    y: str,
    series: Sequence[str],
    p: float = PRICE_PER_POINT,
) -> list[float]:
    """For each series, the average over its plans of X's value minus Y's."""
    plans: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)
    for ref, q in quality.items():
        plans[(ref.arm, ref.series)][ref.meeting] = q
    out = []
    for s in series:
        xq, yq = plans[(x, s)], plans[(y, s)]
        if set(xq) != set(yq):
            raise ValueError(f"{s}: {x} and {y} answered different plans")
        if len(xq) != PLANS_PER_SCORED_SERIES:
            raise ValueError(f"{s}: expected {PLANS_PER_SCORED_SERIES} plans, got {len(xq)}")
        diffs = [(p * xq[m] - dollars[(x, s)]) - (p * yq[m] - dollars[(y, s)]) for m in xq]
        out.append(sum(diffs) / len(diffs))
    return out


def interval(differences: Sequence[float]) -> Interval:
    n = len(differences)
    if n not in T_975:
        raise ValueError(f"an interval needs 4 or 5 series, got {n}")
    mean = statistics.fmean(differences)
    half = T_975[n] * statistics.stdev(differences) / math.sqrt(n)
    return Interval(mean=mean, low=mean - half, high=mean + half)


def compare(
    quality: Mapping[MemoRef, float],
    dollars: Mapping[tuple[str, str], float],
    x: str,
    y: str,
    series: Sequence[str],
    p: float = PRICE_PER_POINT,
) -> Comparison:
    diffs = series_differences(quality, dollars, x, y, series, p)
    return Comparison(x=x, y=y, p=p, differences=tuple(diffs), interval=interval(diffs))


def decide(
    quality: Mapping[MemoRef, float],
    dollars: Mapping[tuple[str, str], float],
    series: Sequence[str],
    *,
    agreement_sufficient: bool,
    run_complete: bool = True,
    scoring_on_time: bool = True,
    judge_cap_reached: bool = False,
    p: float = PRICE_PER_POINT,
) -> Decision:
    """Run the four checks over the series that survived; the first that applies fires.

    ``run_complete`` is False when a spend cap stopped the run or the harness
    failed a third time; ``series`` lists only the series no arm dropped.
    """
    if not run_complete or len(series) < MIN_SERIES:
        return Decision(Outcome.INCOMPLETE, ())
    if not agreement_sufficient or not scoring_on_time or judge_cap_reached:
        return Decision(Outcome.INCONCLUSIVE, ())

    checks: list[Comparison] = []

    def check(x: str, y: str) -> Comparison:
        checks.append(compare(quality, dollars, x, y, series, p))
        return checks[-1]

    if not check("C", "A").clearly_beats:
        outcome = Outcome.RULE_1
    elif not check("D", "C").beats:
        outcome = Outcome.RULE_2
    elif not check("D", "D-prime").beats:
        outcome = Outcome.RULE_3
    else:
        outcome = Outcome.RULE_4
    return Decision(outcome, tuple(checks))


def price_range(
    quality: Mapping[MemoRef, float],
    dollars: Mapping[tuple[str, str], float],
    x: str,
    y: str,
    series: Sequence[str],
    *,
    clearly: bool,
    holds: bool = True,
) -> list[tuple[float, float]]:
    """The prices p ≥ 0 at which "X beats Y" (or "clearly beats") is ``holds``.

    Each series difference is a straight line in p, so the verdict can only
    change where the interval's low end, or its mean less p, crosses zero.
    Those points are found exactly; the verdict is then read between them.
    """
    at0 = series_differences(quality, dollars, x, y, series, 0.0)
    at1 = series_differences(quality, dollars, x, y, series, 1.0)
    u = [b - a for a, b in zip(at0, at1, strict=True)]  # quality difference per series
    v = [-a for a in at0]  # dollar difference per series
    n = len(u)
    k = T_975[n] / math.sqrt(n)
    a, b = statistics.fmean(u), statistics.fmean(v)
    vuu, vvv = statistics.variance(u), statistics.variance(v)
    cuv = statistics.covariance(u, v)

    # low end = 0  ⇔  (a p − b)² = k² · var(p u − v), a quadratic in p
    points = _roots(a * a - k * k * vuu, -2 * a * b + 2 * k * k * cuv, b * b - k * k * vvv)
    if a != 0:
        points.append(b / a)  # the mean crosses zero
    if clearly and a != 1:
        points.append(b / (a - 1))  # the mean crosses p

    def verdict(p: float) -> bool:
        c = compare(quality, dollars, x, y, series, p)
        return (c.clearly_beats if clearly else c.beats) is holds

    return _regions(verdict, points)


def _roots(qa: float, qb: float, qc: float) -> list[float]:
    if abs(qa) < 1e-12:
        return [-qc / qb] if abs(qb) >= 1e-12 else []
    disc = qb * qb - 4 * qa * qc
    if disc < 0:
        return []
    root = math.sqrt(disc)
    return [(-qb - root) / (2 * qa), (-qb + root) / (2 * qa)]


def _regions(verdict: Callable[[float], bool], points: list[float]) -> list[tuple[float, float]]:
    edges = sorted({0.0, *(q for q in points if math.isfinite(q) and q > 0)})
    bounds = [*zip(edges, edges[1:], strict=False), (edges[-1], math.inf)]
    out: list[tuple[float, float]] = []
    for lo, hi in bounds:
        probe = (lo + hi) / 2 if math.isfinite(hi) else lo + 1.0
        if not verdict(probe):
            continue
        if out and out[-1][1] == lo:
            out[-1] = (out[-1][0], hi)
        else:
            out.append((lo, hi))
    return out
