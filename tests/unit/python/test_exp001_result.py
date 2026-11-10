"""EXP-001 harness — the figures part 2 names, from every rater's scores (the scoring PR).

Once the seal is open, every score is read back to the memo it scores, and
the harness works out what part 2 fixes: each memo's quality (§3), the
raters' agreement (§4), the rule that fires (§5, §6), and everything §7
reports without deciding. The arithmetic of each piece is PR 2's
(``test_exp001_scoring.py``, ``test_exp001_decision.py``); these tests check
the pieces are put together as part 2 says.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import pytest

from evaluators.exp001.costs import ARMS
from evaluators.exp001.decision import T_975
from evaluators.exp001.materials import MeetingKind, Series, load_materials
from evaluators.exp001.rating import PEOPLE, RATERS
from evaluators.exp001.result import Ratings, build
from evaluators.exp001.result_report import summary
from evaluators.exp001.scoring import MemoRef

from ._exp001_run_test_helpers import EXP
from ._exp001_scores_test_helpers import reversed_scorer, table_scorer

SERIES: tuple[Series, ...] = load_materials(EXP).series[:4]
_Scorer = Callable[[str, str, bool], dict[str, int | None]]


def _ratings(scorers: Mapping[str, _Scorer], *, missing: tuple[MemoRef, ...] = ()) -> Ratings:
    """Every arm's memos and recall replies of the four series, each rater
    scoring by its scorer; D's and D′'s recall answers are right."""
    memos, recall, packets = {}, {}, {}
    for s in SERIES:
        for arm in ARMS:
            for meeting in s.meetings:
                ref = MemoRef(arm, s.id, meeting.id)
                if ref in missing or meeting.kind is MeetingKind.BRIEFING:
                    continue
                packets[ref] = f"{arm}/{meeting.id}"
                if meeting.kind is MeetingKind.RECALL:
                    assert meeting.recall_key is not None
                    marks = dict.fromkeys(meeting.recall_key, arm in ("D", "D-prime"))
                    recall[ref] = dict.fromkeys(scorers, marks)
                else:
                    control = meeting.kind is MeetingKind.CONTROL
                    memos[ref] = {r: f(arm, s.id, control) for r, f in scorers.items()}
    return Ratings(memos=memos, recall=recall, packets=packets)


_TABLE = dict.fromkeys(RATERS, table_scorer)
# Every arm costs the same, but B, which costs a dollar more a plan.
_DOLLARS = {(arm, s.id): 2.0 if arm == "B" else 1.0 for arm in ARMS for s in SERIES}


def _build(ratings: Ratings | None = None, **kwargs: Any) -> dict[str, Any]:
    figures: dict[str, Any] = {
        "series": SERIES, "arms": ARMS, "ratings": ratings or _ratings(_TABLE),
        "raters": RATERS, "dollars": _DOLLARS, "repriced": _DOLLARS,
        "minutes": {}, "run_complete": True, "on_time": True, "judge_cap_reached": False,
    }
    figures.update(kwargs)
    return build(**figures)


class TestEachArm:
    def test_quality_criteria_and_control_plans(self) -> None:
        d = _build()["arms"]["D-prime"]
        # D′: 2, 1, 2, 2, 2 on its three plans; 2, -, 2, 2, 2 = 10 on its control plan.
        assert d["plans_quality"] == pytest.approx(9.0)
        assert d["control_quality"] == pytest.approx(10.0)
        assert d["quality"] == pytest.approx((9.0 * 3 + 10.0) / 4)
        assert d["criteria"] == pytest.approx({"C1": 2, "C2": 1, "C3": 2, "C4": 2, "C5": 2})

    def test_a_missing_memo_scores_zero_on_every_criterion(self) -> None:
        plan = SERIES[0].scored_meetings[0]
        assert plan.kind is MeetingKind.PLAN
        ratings = _ratings(_TABLE, missing=(MemoRef("D", SERIES[0].id, plan.id),))
        d = _build(ratings)["arms"]["D"]
        assert d["quality"] == pytest.approx(10.0 * 15 / 16)
        assert d["criteria"]["C1"] == pytest.approx(2.0 * 15 / 16)
        assert d["criteria"]["C2"] == pytest.approx(2.0 * 11 / 12)  # C2: plans, not control

    def test_quality_per_dollar_and_per_minute(self) -> None:
        minutes = {MemoRef("B", s.id, m.id): 4.0 for s in SERIES for m in s.scored_meetings}
        b = _build(minutes=minutes)["arms"]["B"]
        assert b["dollars_per_plan"] == pytest.approx(2.0)
        assert b["quality_per_dollar"] == pytest.approx(b["quality"] / 2.0)
        assert (b["minutes_per_plan"], b["minutes_left_out"]) == (4.0, 0)
        assert b["quality_per_minute"] == pytest.approx(b["quality"] / 4.0)

    def test_a_recall_answer_is_right_when_two_raters_say_so(self) -> None:
        arms = _build()["arms"]
        assert arms["D"]["recall"] == {"right": 16, "asked": 16}
        assert arms["C"]["recall"] == {"right": 0, "asked": 16}


class TestTheComparisons:
    def test_every_comparison_with_every_series_difference(self) -> None:
        result = _build()
        pairs = [(c["x"], c["y"], c["deciding"]) for c in result["comparisons"]]
        assert pairs == [("C", "A", True), ("D", "C", True), ("D", "D-prime", True),
                         ("B", "A", False), ("C", "B", False)]
        for c in result["comparisons"]:
            assert len(c["differences"]) == 4
        assert result["outcome"] == "rule 4"

    def test_four_series_take_t_for_three_degrees_of_freedom(self) -> None:
        c = _build()["comparisons"][0]
        spread = c["interval"]["mean"] - c["interval"]["low"]
        diffs = c["differences"]
        mean = sum(diffs) / 4
        sd = (sum((d - mean) ** 2 for d in diffs) / 3) ** 0.5
        assert spread == pytest.approx(T_975[4] * sd / 2)

    def test_each_deciding_verdict_has_its_price_range_and_its_repriced_verdict(self) -> None:
        # Repriced, C costs $4 a plan more than A: more than C's lead in value.
        repriced = {**_DOLLARS, **{("C", s.id): 5.0 for s in SERIES}}
        result = _build(repriced=repriced)
        check_1 = result["comparisons"][0]
        assert check_1["verdict"] == {"name": "clearly beats", "holds": True}
        assert check_1["price_range"] == [[0.0, None]]
        assert check_1["repriced"]["clearly_beats"] is False
        assert result["repriced_outcome"] == "rule 1"
        b_against_a = result["comparisons"][3]
        assert b_against_a["price_range"] is None and b_against_a["verdict"] is None


class TestWhenNoRuleFires:
    def test_fewer_than_four_series_is_incomplete_with_no_comparison(self) -> None:
        result = _build(series=SERIES[:3])
        assert (result["outcome"], result["why"]) == ("incomplete", "fewer than four series")
        assert result["comparisons"] == []

    def test_an_incomplete_run_fires_no_rule(self) -> None:
        result = _build(run_complete=False)
        assert (result["outcome"], result["why"]) == ("incomplete", "the run is incomplete")

    def test_every_cause_that_holds_is_named(self) -> None:
        result = _build(on_time=False, judge_cap_reached=True)
        assert (result["outcome"], result["why"]) == (
            "inconclusive",
            "scoring finished after its 21 days; the judge's spend reached its cap",
        )
        # The judge's cap does not bear on the rule on the people alone.
        assert result["people_alone"]["why"] == "scoring finished after its 21 days"

    def test_the_rule_on_the_peoples_scores_alone(self) -> None:
        # The judge reads the table upside down: three raters cannot agree,
        # but the two people do, and alone they fire the table's rule.
        scorers = {**dict.fromkeys(PEOPLE, table_scorer), "judge": reversed_scorer}
        result = _build(_ratings(scorers))
        assert result["outcome"] == "inconclusive"
        alone = result["people_alone"]
        assert (alone["outcome"], alone["agreement"]) == ("rule 4", pytest.approx(1.0))
        # Named by their sheets, since two AI raters fill them in the people's place.
        held = {"run": "scored", "window": 1, "opened_at": "2026-11-27T00:00:00+00:00",
                "scoring_due": None, "meetings": []}
        text = summary({**result, **held})
        assert "  the rule on person-1's and person-2's sheets alone: rule 4" in text
