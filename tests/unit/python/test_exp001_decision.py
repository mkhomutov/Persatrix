"""EXP-001 harness — the interval, the verdicts and the rule that fires.

Part 2 §5 of the pre-registration fixes the arithmetic: value at p = $1 a
quality point, series differences, a t-interval over series, "beats" and
"clearly beats", four checks in order. §6 says an incomplete run or an
inconclusive score fires no rule, and §7 asks for the range of prices over
which each deciding verdict holds.
"""

from __future__ import annotations

import pytest

from evaluators.exp001.decision import (
    PRICE_PER_POINT,
    T_975,
    Outcome,
    compare,
    decide,
    interval,
    price_range,
    series_differences,
)
from evaluators.exp001.scoring import MemoRef

SERIES = tuple(f"series-{n}" for n in range(1, 6))
PLANS = ("p1", "p2", "p3", "p4")
# Small series-to-series wobble, so intervals have width.
WOBBLE = (0.0, 0.2, -0.2, 0.1, -0.1)


def _arm(
    arm: str, quality: float, dollars: float, series: tuple[str, ...] = SERIES
) -> tuple[dict[MemoRef, float], dict[tuple[str, str], float]]:
    q = {
        MemoRef(arm, s, f"{s}-{p}"): quality + WOBBLE[i] * (1 if arm in ("C", "D") else -1)
        for i, s in enumerate(series)
        for p in PLANS
    }
    d = {(arm, s): dollars for s in series}
    return q, d


def _run(
    arms: dict[str, tuple[float, float]], series: tuple[str, ...] = SERIES
) -> tuple[dict[MemoRef, float], dict[tuple[str, str], float]]:
    quality: dict[MemoRef, float] = {}
    dollars: dict[tuple[str, str], float] = {}
    for arm, (q, d) in arms.items():
        aq, ad = _arm(arm, q, d, series)
        quality |= aq
        dollars |= ad
    return quality, dollars


def test_price_of_a_quality_point_is_one_dollar() -> None:
    assert PRICE_PER_POINT == 1.00


def test_series_difference_averages_value_over_the_series_plans() -> None:
    quality = {
        **{MemoRef("X", "series-1", f"m{i}"): q for i, q in enumerate([6, 7, 8, 9])},
        **{MemoRef("Y", "series-1", f"m{i}"): 5.0 for i in range(4)},
    }
    dollars = {("X", "series-1"): 0.5, ("Y", "series-1"): 0.1}

    # (7.5 p - 0.5) - (5 p - 0.1)
    assert series_differences(quality, dollars, "X", "Y", ("series-1",)) == pytest.approx([2.1])
    assert series_differences(quality, dollars, "X", "Y", ("series-1",), p=2.0) == pytest.approx(
        [4.6]
    )


def test_series_difference_refuses_arms_that_answered_different_plans() -> None:
    quality = {
        **{MemoRef("X", "series-1", f"m{i}"): 5.0 for i in range(4)},
        **{MemoRef("Y", "series-1", f"m{i}"): 5.0 for i in range(3)},
    }
    dollars = {("X", "series-1"): 0.0, ("Y", "series-1"): 0.0}
    with pytest.raises(ValueError, match="series-1"):
        series_differences(quality, dollars, "X", "Y", ("series-1",))


def test_series_difference_refuses_a_series_without_four_plans() -> None:
    quality = {
        **{MemoRef("X", "series-1", f"m{i}"): 5.0 for i in range(3)},
        **{MemoRef("Y", "series-1", f"m{i}"): 5.0 for i in range(3)},
    }
    dollars = {("X", "series-1"): 0.0, ("Y", "series-1"): 0.0}
    with pytest.raises(ValueError, match="4 plans"):
        series_differences(quality, dollars, "X", "Y", ("series-1",))


def test_interval_is_mean_plus_or_minus_t_sd_over_root_n() -> None:
    assert T_975 == {5: 2.776, 4: 3.182}  # keyed by the number of series
    got = interval([1.0, 2.0, 3.0, 4.0, 5.0])
    half = 2.776 * 2.5**0.5 / 5**0.5
    assert (got.mean, got.low, got.high) == pytest.approx((3.0, 3.0 - half, 3.0 + half))


def test_interval_over_four_series_uses_t_for_three_degrees_of_freedom() -> None:
    got = interval([1.0, 2.0, 3.0, 4.0])
    half = 3.182 * (5 / 3) ** 0.5 / 2
    assert (got.low, got.high) == pytest.approx((2.5 - half, 2.5 + half))


@pytest.mark.parametrize("n", [3, 6])
def test_interval_refuses_a_series_count_the_run_cannot_produce(n: int) -> None:
    with pytest.raises(ValueError, match="series"):
        interval([1.0] * n)


def test_beats_needs_the_whole_interval_above_zero() -> None:
    quality, dollars = _run({"C": (6.0, 0.0), "A": (5.0, 0.0)})
    result = compare(quality, dollars, "C", "A", SERIES)
    assert result.interval.low > 0
    assert result.beats is True
    assert result.clearly_beats is True  # mean difference 1.0 == p


def test_beats_but_not_clearly_when_the_mean_difference_is_under_p() -> None:
    quality, dollars = _run({"C": (5.9, 0.0), "A": (5.0, 0.0)})
    result = compare(quality, dollars, "C", "A", SERIES)
    assert (result.beats, result.clearly_beats) == (True, False)


def test_dollars_count_against_an_arm() -> None:
    # One quality point better, but $1.50 a plan dearer: worse in value.
    quality, dollars = _run({"C": (6.0, 1.5), "A": (5.0, 0.0)})
    result = compare(quality, dollars, "C", "A", SERIES)
    assert result.interval.mean == pytest.approx(-0.5)
    assert result.beats is False


def test_an_interval_touching_zero_does_not_beat() -> None:
    quality = {
        **{
            MemoRef("X", s, f"{s}-{p}"): 5.0 + (1.0 if s == "series-1" else 0.0)
            for s in SERIES
            for p in PLANS
        },
        **{MemoRef("Y", s, f"{s}-{p}"): 5.0 for s in SERIES for p in PLANS},
    }
    dollars = {(a, s): 0.0 for a in "XY" for s in SERIES}
    result = compare(quality, dollars, "X", "Y", SERIES)
    assert result.interval.mean > 0
    assert result.interval.low < 0
    assert result.beats is False


_GOOD = {"A": (5.0, 0.01), "B": (5.5, 0.2)}


@pytest.mark.parametrize(
    ("arms", "outcome"),
    [
        # C no better than A.
        ({**_GOOD, "C": (5.0, 0.3), "D": (8.0, 0.4), "D-prime": (6.0, 0.4)}, Outcome.RULE_1),
        # C beats A, but not clearly: rule 1 still comes first.
        ({**_GOOD, "C": (5.8, 0.3), "D": (8.0, 0.4), "D-prime": (6.0, 0.4)}, Outcome.RULE_1),
        # C clearly beats A; D no better than C.
        ({**_GOOD, "C": (7.0, 0.3), "D": (7.0, 0.4), "D-prime": (6.0, 0.4)}, Outcome.RULE_2),
        # D beats C; D-prime as good as D.
        ({**_GOOD, "C": (7.0, 0.3), "D": (8.0, 0.4), "D-prime": (8.0, 0.4)}, Outcome.RULE_3),
        # D beats D-prime.
        ({**_GOOD, "C": (7.0, 0.3), "D": (8.0, 0.4), "D-prime": (6.5, 0.4)}, Outcome.RULE_4),
    ],
)
def test_the_first_check_that_applies_fires_its_rule(
    arms: dict[str, tuple[float, float]], outcome: Outcome
) -> None:
    quality, dollars = _run(arms)
    decision = decide(quality, dollars, SERIES, agreement_sufficient=True)
    assert decision.outcome is outcome


def test_decide_reports_the_checks_it_read_in_order() -> None:
    quality, dollars = _run({**_GOOD, "C": (7.0, 0.3), "D": (8.0, 0.4), "D-prime": (6.5, 0.4)})
    decision = decide(quality, dollars, SERIES, agreement_sufficient=True)
    assert [(c.x, c.y) for c in decision.checks] == [("C", "A"), ("D", "C"), ("D", "D-prime")]


def test_decide_stops_at_the_check_that_fires() -> None:
    quality, dollars = _run({**_GOOD, "C": (5.0, 0.3), "D": (8.0, 0.4), "D-prime": (6.0, 0.4)})
    decision = decide(quality, dollars, SERIES, agreement_sufficient=True)
    assert [(c.x, c.y) for c in decision.checks] == [("C", "A")]


_RULE_4 = {**_GOOD, "C": (7.0, 0.3), "D": (8.0, 0.4), "D-prime": (6.5, 0.4)}


@pytest.mark.parametrize(
    ("kwargs", "outcome"),
    [
        ({"agreement_sufficient": False}, Outcome.INCONCLUSIVE),
        ({"agreement_sufficient": True, "scoring_on_time": False}, Outcome.INCONCLUSIVE),
        ({"agreement_sufficient": True, "judge_cap_reached": True}, Outcome.INCONCLUSIVE),
        ({"agreement_sufficient": True, "run_complete": False}, Outcome.INCOMPLETE),
        # An incomplete run is reported as incomplete, whatever the scores.
        ({"agreement_sufficient": False, "run_complete": False}, Outcome.INCOMPLETE),
    ],
)
def test_no_rule_fires_on_an_incomplete_run_or_an_inconclusive_score(
    kwargs: dict[str, bool], outcome: Outcome
) -> None:
    quality, dollars = _run(_RULE_4)
    decision = decide(quality, dollars, SERIES, **kwargs)
    assert decision.outcome is outcome
    assert decision.checks == ()


def test_fewer_than_four_surviving_series_is_incomplete() -> None:
    quality, dollars = _run(_RULE_4, SERIES[:3])
    decision = decide(quality, dollars, SERIES[:3], agreement_sufficient=True)
    assert decision.outcome is Outcome.INCOMPLETE


def test_four_surviving_series_still_decide() -> None:
    quality, dollars = _run(_RULE_4, SERIES[:4])
    decision = decide(quality, dollars, SERIES[:4], agreement_sufficient=True)
    assert decision.outcome is Outcome.RULE_4


# --- §7 the price range -------------------------------------------------------


def _flat(dq: float, dd: float) -> tuple[dict[MemoRef, float], dict[tuple[str, str], float]]:
    """X is dq quality points better and dd dollars a plan dearer, in every series."""
    quality = {
        **{MemoRef("X", s, f"{s}-{p}"): 5.0 + dq for s in SERIES for p in PLANS},
        **{MemoRef("Y", s, f"{s}-{p}"): 5.0 for s in SERIES for p in PLANS},
    }
    dollars = {**{("X", s): dd for s in SERIES}, **{("Y", s): 0.0 for s in SERIES}}
    return quality, dollars


def test_price_range_where_x_beats_y_with_no_spread() -> None:
    quality, dollars = _flat(2.0, 1.0)  # value difference 2p - 1
    assert price_range(quality, dollars, "X", "Y", SERIES, clearly=False) == pytest.approx(
        [(0.5, float("inf"))]
    )
    # clearly: 2p - 1 >= p
    assert price_range(quality, dollars, "X", "Y", SERIES, clearly=True) == pytest.approx(
        [(1.0, float("inf"))]
    )


def test_price_range_where_the_verdict_fails() -> None:
    quality, dollars = _flat(2.0, 1.0)
    assert price_range(
        quality, dollars, "X", "Y", SERIES, clearly=False, holds=False
    ) == pytest.approx([(0.0, 0.5)])


def test_price_range_edges_are_where_the_verdict_flips() -> None:
    quality, dollars = _run({"C": (6.0, 0.5), "A": (5.0, 0.0)})
    ranges = price_range(quality, dollars, "C", "A", SERIES, clearly=False)
    assert len(ranges) == 1
    low, high = ranges[0]
    assert 0 < low < high

    def beats(p: float) -> bool:
        return compare(quality, dollars, "C", "A", SERIES, p=p).beats

    assert not beats(low * (1 - 1e-6))
    assert beats(low * (1 + 1e-6))
    if high != float("inf"):
        assert beats(high * (1 - 1e-6))
        assert not beats(high * (1 + 1e-6))


def test_an_arm_does_not_beat_its_equal() -> None:
    """An interval of exactly zero is not above zero."""
    quality, dollars = _flat(0.0, 0.0)
    result = compare(quality, dollars, "X", "Y", SERIES)
    assert (result.interval.low, result.interval.high) == (0.0, 0.0)
    assert result.beats is False
