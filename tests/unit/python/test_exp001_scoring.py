"""EXP-001 harness — the 400-word cut, memo quality, recall majority, agreement.

Part 2 of the pre-registration fixes these: §1 the cut, §3 the rubric totals
and the three-rater average, the two-of-three recall rule, and §4 the
per-plan agreement figure that decides whether the scores can carry a rule.
"""

from __future__ import annotations

import pytest

from evaluators.exp001.materials import MeetingKind
from evaluators.exp001.scoring import (
    AGREEMENT_FLOOR,
    WORD_LIMIT,
    MemoRef,
    agreement,
    cut_memo,
    memo_quality,
    rater_total,
    recall_right,
    spearman,
)

RATERS = ("person-1", "person-2", "judge")


def _scores(c1: int, c2: int | None, c3: int, c4: int, c5: int) -> dict[str, int | None]:
    return {"C1": c1, "C2": c2, "C3": c3, "C4": c4, "C5": c5}


# --- §1 the cut -------------------------------------------------------------


def test_cut_memo_keeps_a_short_memo_whole() -> None:
    cut = cut_memo("Recommend B.\n\nBecause the lease ends in June.")
    assert (cut.text, cut.words, cut.cut) == (
        "Recommend B.\n\nBecause the lease ends in June.",
        8,
        False,
    )


def test_cut_memo_cuts_after_the_400th_word_and_records_where() -> None:
    words = [f"w{i}" for i in range(1, 431)]
    text = "## Memo\n" + "  ".join(words[:200]) + "\n\n" + " ".join(words[200:])

    cut = cut_memo(text)

    assert WORD_LIMIT == 400
    assert cut.cut is True
    assert cut.words == 432  # "## Memo" is two words, the heading counts
    assert cut.text.split()[-1] == "w398"
    assert len(cut.text.split()) == 400
    assert cut.text == text[: len(cut.text)]  # formatting kept up to the cut


def test_cut_memo_of_exactly_400_words_is_not_cut() -> None:
    cut = cut_memo(" ".join(["x"] * 400) + "\n")
    assert (cut.words, cut.cut) == (400, False)


# --- §3 totals and quality ----------------------------------------------------


def test_rater_total_adds_five_criteria_on_a_plan() -> None:
    assert rater_total(_scores(2, 1, 2, 0, 1), MeetingKind.PLAN) == 6.0


def test_rater_total_scales_four_criteria_to_ten_on_a_control_plan() -> None:
    assert rater_total(_scores(2, None, 1, 1, 2), MeetingKind.CONTROL) == pytest.approx(7.5)


@pytest.mark.parametrize(
    ("scores", "kind", "message"),
    [
        (_scores(3, 1, 1, 1, 1), MeetingKind.PLAN, "C1"),
        (_scores(1, None, 1, 1, 1), MeetingKind.PLAN, "C2"),
        (_scores(1, 1, 1, 1, 1), MeetingKind.CONTROL, "C2"),
        ({"C1": 1, "C2": 1, "C3": 1, "C4": 1}, MeetingKind.PLAN, "C5"),
    ],
)
def test_rater_total_refuses_scores_the_rubric_does_not_allow(
    scores: dict[str, int | None], kind: MeetingKind, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        rater_total(scores, kind)


def test_memo_quality_is_the_average_of_the_three_raters_totals() -> None:
    by_rater = {
        "person-1": _scores(2, 2, 2, 2, 2),  # 10
        "person-2": _scores(1, 1, 1, 1, 1),  # 5
        "judge": _scores(2, 0, 2, 1, 1),  # 6
    }
    assert memo_quality(by_rater, MeetingKind.PLAN, RATERS) == pytest.approx(7.0)


def test_memo_quality_of_a_missing_memo_is_zero() -> None:
    assert memo_quality(None, MeetingKind.PLAN, RATERS) == 0.0


def test_memo_quality_reads_only_the_raters_named() -> None:
    """The report's 'two people alone' figure uses the same arithmetic."""
    by_rater = {
        "person-1": _scores(2, 2, 2, 2, 2),
        "person-2": _scores(1, 1, 1, 1, 1),
        "judge": _scores(0, 0, 0, 0, 0),
    }
    assert memo_quality(by_rater, MeetingKind.PLAN, RATERS[:2]) == pytest.approx(7.5)


def test_memo_quality_refuses_a_rater_who_has_not_scored() -> None:
    with pytest.raises(ValueError, match="judge"):
        memo_quality({"person-1": _scores(1, 1, 1, 1, 1)}, MeetingKind.PLAN, ("person-1", "judge"))


@pytest.mark.parametrize(
    ("marks", "right"),
    [
        ((True, True, True), True),
        ((True, True, False), True),
        ((False, True, True), True),
        ((True, False, False), False),
        ((False, False, False), False),
    ],
)
def test_recall_answer_is_right_when_two_of_three_raters_say_so(
    marks: tuple[bool, bool, bool], right: bool
) -> None:
    assert recall_right(marks) is right


def test_recall_right_needs_exactly_three_marks() -> None:
    with pytest.raises(ValueError, match="three"):
        recall_right((True, True))


# --- §4 agreement -------------------------------------------------------------


def test_spearman_of_identical_and_reversed_rankings() -> None:
    assert spearman([1, 2, 3, 4, 5], [2, 4, 6, 8, 10]) == pytest.approx(1.0)
    assert spearman([1, 2, 3, 4, 5], [5, 4, 3, 2, 1]) == pytest.approx(-1.0)


def test_spearman_gives_tied_totals_their_average_rank() -> None:
    # ranks [1, 2.5, 2.5, 4] against [1, 2, 3, 4]: 4.5 / sqrt(4.5 * 5)
    assert spearman([1, 2, 2, 3], [1, 2, 3, 4]) == pytest.approx(4.5 / (4.5 * 5) ** 0.5)


def test_spearman_is_undefined_when_a_rater_gives_every_memo_one_total() -> None:
    assert spearman([5, 5, 5], [1, 2, 3]) is None


def _plan_totals(
    series: str, meeting: str, per_rater: dict[str, list[float]]
) -> dict[MemoRef, dict[str, float]]:
    arms = ("A", "B", "C", "D", "D-prime")
    out: dict[MemoRef, dict[str, float]] = {}
    for i, arm in enumerate(arms[: len(next(iter(per_rater.values())))]):
        out[MemoRef(arm, series, meeting)] = {r: totals[i] for r, totals in per_rater.items()}
    return out


def test_agreement_ranks_within_each_plan_and_averages_over_plans() -> None:
    totals = {
        # plan 1: everyone ranks the same way
        **_plan_totals(
            "series-1",
            "p1",
            {"person-1": [1, 2, 3, 4, 5], "person-2": [2, 3, 4, 5, 6], "judge": [0, 1, 2, 3, 4]},
        ),
        # plan 2: person-2 reverses the other two
        **_plan_totals(
            "series-1",
            "p2",
            {"person-1": [1, 2, 3, 4, 5], "person-2": [5, 4, 3, 2, 1], "judge": [1, 2, 3, 4, 5]},
        ),
    }

    result = agreement(totals, RATERS)

    assert result.pairs[("person-1", "person-2")].mean == pytest.approx(0.0)
    assert result.pairs[("person-1", "judge")].mean == pytest.approx(1.0)
    assert result.pairs[("person-2", "judge")].mean == pytest.approx(0.0)
    assert result.mean == pytest.approx(1 / 3)
    assert AGREEMENT_FLOOR == 0.4
    assert result.sufficient is False


def test_agreement_hides_nothing_that_pooling_would() -> None:
    """Raters who agree only on which plan is hard look fine pooled, not per plan."""
    easy: dict[str, list[float]] = {
        "person-1": [8, 9, 10],
        "person-2": [10, 9, 8],
        "judge": [9, 8, 10],
    }
    hard: dict[str, list[float]] = {
        "person-1": [0, 1, 2],
        "person-2": [2, 1, 0],
        "judge": [1, 0, 2],
    }
    totals = {**_plan_totals("series-1", "easy", easy), **_plan_totals("series-1", "hard", hard)}

    result = agreement(totals, RATERS)

    pooled = result.pooled[("person-1", "person-2")]
    assert pooled is not None and pooled > 0.5
    assert result.pairs[("person-1", "person-2")].mean == pytest.approx(-1.0)


def test_agreement_skips_a_plan_with_fewer_than_three_memos() -> None:
    totals = {
        **_plan_totals(
            "series-1", "p1", {"person-1": [1, 2, 3], "person-2": [1, 2, 3], "judge": [1, 2, 3]}
        ),
        **_plan_totals("series-1", "p2", {"person-1": [1, 2], "person-2": [2, 1], "judge": [2, 1]}),
    }

    pair = agreement(totals, RATERS).pairs[("person-1", "person-2")]

    assert (pair.mean, pair.plans_used, pair.plans_skipped) == (pytest.approx(1.0), 1, 1)


def test_agreement_skips_a_plan_a_rater_could_not_rank_and_counts_it() -> None:
    totals = {
        **_plan_totals(
            "series-1", "p1", {"person-1": [1, 2, 3], "person-2": [1, 2, 3], "judge": [1, 2, 3]}
        ),
        **_plan_totals(
            "series-1", "p2", {"person-1": [4, 4, 4], "person-2": [1, 2, 3], "judge": [3, 2, 1]}
        ),
    }

    result = agreement(totals, RATERS)

    assert result.pairs[("person-1", "person-2")].plans_skipped == 1
    assert result.pairs[("person-2", "judge")].plans_used == 2
    assert result.pairs[("person-2", "judge")].mean == pytest.approx(0.0)


def test_agreement_with_no_plan_left_for_a_pair_cannot_carry_a_decision() -> None:
    totals = _plan_totals(
        "series-1", "p1", {"person-1": [4, 4, 4], "person-2": [1, 2, 3], "judge": [1, 2, 3]}
    )

    result = agreement(totals, RATERS)

    assert result.pairs[("person-1", "judge")].mean is None
    assert result.mean is None
    assert result.sufficient is False


@pytest.mark.parametrize(
    ("judge", "sufficient"),
    [
        # sum of squared rank gaps 18: rho = 1 - 6*18/120 = 0.1, so (1 + 0.1 + 0.1) / 3 = 0.4
        ([2, 5, 1, 4, 3], True),
        # sum 20: rho = 0.0, so the average is 1/3
        ([2, 5, 3, 1, 4], False),
    ],
)
def test_agreement_exactly_at_the_floor_is_sufficient(judge: list[float], sufficient: bool) -> None:
    """'Below 0.4' is inconclusive; a float that misses 0.4 by rounding is not below it."""
    totals = _plan_totals(
        "series-1",
        "p1",
        {"person-1": [1, 2, 3, 4, 5], "person-2": [1, 2, 3, 4, 5], "judge": judge},
    )

    assert agreement(totals, RATERS).sufficient is sufficient
