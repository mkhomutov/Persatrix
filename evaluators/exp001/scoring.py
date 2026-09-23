"""How EXP-001 turns the raters' scores into the numbers the decision reads.

Three raters, two people and an LLM judge, score every memo on the rubric's
five criteria and mark every recall answer right or wrong. This module does
the arithmetic part 2 of the pre-registration fixes:

- the 400-word cut every memo gets before anyone scores it (§1);
- a rater's total and a memo's **quality**, the three raters' average (§3);
- a recall answer counts as right when two of the three raters say so (§3);
- **agreement**: how closely each pair of raters ranks the five memos of the
  same plan, averaged over the plans (§4). Below 0.4 the scores cannot carry
  a decision.
"""

from __future__ import annotations

import itertools
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import NamedTuple

from evaluators.exp001.materials import MeetingKind

WORD_LIMIT = 400
CRITERIA = ("C1", "C2", "C3", "C4", "C5")
MEMORY_CRITERION = "C2"
AGREEMENT_FLOOR = 0.4
MIN_MEMOS_PER_PLAN = 3
# Agreement is compared with the floor after rounding, so a figure of exactly
# 0.4 that floating point lands a hair under is not "below 0.4".
_AGREEMENT_DIGITS = 9

_WORD = re.compile(r"\S+")


class MemoRef(NamedTuple):
    """Which memo: the arm that wrote it, and the series and plan it answers."""

    arm: str
    series: str
    meeting: str


@dataclass(frozen=True)
class CutMemo:
    text: str  # the memo as the raters see it
    words: int  # words in the memo as written
    cut: bool  # whether the harness cut it


def cut_memo(text: str) -> CutMemo:
    """Keep the memo up to the end of its 400th word; a word is any run of non-spaces."""
    words = list(_WORD.finditer(text))
    if len(words) <= WORD_LIMIT:
        return CutMemo(text=text, words=len(words), cut=False)
    return CutMemo(text=text[: words[WORD_LIMIT - 1].end()], words=len(words), cut=True)


def rater_total(scores: Mapping[str, int | None], kind: MeetingKind) -> float:
    """One rater's total out of 10; a control plan's four criteria are scaled by 10/8."""
    control = kind is MeetingKind.CONTROL
    total = 0
    for criterion in CRITERIA:
        if criterion not in scores:
            raise ValueError(f"no score for {criterion}")
        score = scores[criterion]
        if criterion == MEMORY_CRITERION and control:
            if score is not None:
                raise ValueError(f"{criterion} is not scored on a control plan")
            continue
        # type() rather than isinstance(): True and False are ints in Python.
        if type(score) is not int or score not in (0, 1, 2):
            raise ValueError(f"{criterion} must be 0, 1 or 2, not {score!r}")
        total += score
    return total * 10 / 8 if control else float(total)


def memo_quality(
    by_rater: Mapping[str, Mapping[str, int | None]] | None,
    kind: MeetingKind,
    raters: Sequence[str],
) -> float:
    """The average of the named raters' totals; a memo that was never written scores 0."""
    if by_rater is None:
        return 0.0
    missing = [r for r in raters if r not in by_rater]
    if missing:
        raise ValueError(f"no scores from {', '.join(missing)}")
    return sum(rater_total(by_rater[r], kind) for r in raters) / len(raters)


def recall_right(marks: Sequence[bool]) -> bool:
    if len(marks) != 3:
        raise ValueError(f"a recall answer needs three raters' marks, got {len(marks)}")
    return sum(marks) >= 2


def spearman(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Rank correlation, tied values sharing their average rank.

    ``None`` when either side gives every item the same value, since such a
    ranking says nothing about order.
    """
    rx, ry = _ranks(xs), _ranks(ys)
    n = len(rx)
    mx, my = sum(rx) / n, sum(ry) / n
    sxy = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    sxx = sum((a - mx) ** 2 for a in rx)
    syy = sum((b - my) ** 2 for b in ry)
    if sxx == 0 or syy == 0:
        return None
    return float(sxy / (sxx * syy) ** 0.5)


def _ranks(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


@dataclass(frozen=True)
class PairAgreement:
    mean: float | None  # average per-plan correlation; None when no plan could be ranked
    plans_used: int
    plans_skipped: int  # too few memos, or one rater gave all of them one total


@dataclass(frozen=True)
class Agreement:
    pairs: dict[tuple[str, str], PairAgreement]
    pooled: dict[tuple[str, str], float | None]  # over all memos at once; reported only
    mean: float | None

    @property
    def sufficient(self) -> bool:
        return self.mean is not None and round(self.mean, _AGREEMENT_DIGITS) >= AGREEMENT_FLOOR


def agreement(totals: Mapping[MemoRef, Mapping[str, float]], raters: Sequence[str]) -> Agreement:
    """Per-plan rank agreement for each pair of raters, and their average.

    ``totals`` holds each rater's total for every memo that exists; the caller
    leaves out memos the harness scored 0 because none was written.
    """
    plans: dict[tuple[str, str], list[MemoRef]] = defaultdict(list)
    for ref in totals:
        plans[(ref.series, ref.meeting)].append(ref)

    pairs: dict[tuple[str, str], PairAgreement] = {}
    pooled: dict[tuple[str, str], float | None] = {}
    for a, b in itertools.combinations(raters, 2):
        correlations: list[float] = []
        skipped = 0
        for refs in plans.values():
            rho = None
            if len(refs) >= MIN_MEMOS_PER_PLAN:
                rho = spearman([totals[r][a] for r in refs], [totals[r][b] for r in refs])
            if rho is None:
                skipped += 1
            else:
                correlations.append(rho)
        mean = sum(correlations) / len(correlations) if correlations else None
        pairs[(a, b)] = PairAgreement(mean, len(correlations), skipped)
        refs_all = list(totals)
        pooled[(a, b)] = spearman(
            [totals[r][a] for r in refs_all], [totals[r][b] for r in refs_all]
        )

    means = [p.mean for p in pairs.values()]
    overall = None if any(m is None for m in means) else sum(means) / len(means)  # type: ignore[arg-type]
    return Agreement(pairs=pairs, pooled=pooled, mean=overall)
