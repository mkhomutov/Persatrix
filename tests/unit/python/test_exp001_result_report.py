"""EXP-001 harness — the scoring's plain words (the scoring PR).

``scores.txt`` gives each meeting in a line, and scoring's progress gives
each rater in one. These tests hold the words to what happened: a discussion
that never closed is not "one call", which is arm A's alone; a memo never
written is named; and a judge its cap stopped has not scored every packet.
The figures themselves are in ``test_exp001_result.py``.
"""

from __future__ import annotations

from typing import Any

from evaluators.exp001.result_report import meeting_words, progress_words
from evaluators.exp001.scoring_run import Progress


def _row(**changed: Any) -> dict[str, Any]:
    """One meeting's row of the result: a channel arm's plan, closed by a vote."""
    row = {
        "arm": "C", "series": "series-1", "meeting": "series-1-plan-1", "kind": "plan",
        "recorded": True, "answered": True, "closed_by": "vote",
        "energy": {"lunar-stoat": 0.8, "velvet-pika": None}, "minutes": 6.04,
        "words": 380, "cut": False,
    }
    return {**row, **changed}


class TestEachMeeting:
    def test_a_discussion_says_what_closed_it(self) -> None:
        assert meeting_words(_row()) == (
            "closed by vote; energy lunar-stoat 0.80, velvet-pika none; 6.0 minutes"
        )

    def test_a_memo_cut_says_so(self) -> None:
        assert meeting_words(_row(words=410, cut=True)).endswith("memo cut at 400 of 410 words")

    def test_only_arm_a_holds_one_call(self) -> None:
        arm_a = _row(arm="A", closed_by=None, energy=None, minutes=0.33)
        assert meeting_words(arm_a) == "one call; 0.3 minutes"

    def test_a_discussion_that_never_closed_says_so(self) -> None:
        never = _row(closed_by=None, energy={}, minutes=None, answered=False, words=None,
                     cut=None)
        assert meeting_words(never) == "never closed; no memo, scored 0"

    def test_a_try_with_no_record_says_so(self) -> None:
        lost = _row(recorded=False, closed_by=None, energy=None, minutes=None, answered=False,
                    words=None, cut=None)
        assert meeting_words(lost) == "no record; no memo, scored 0"

    def test_a_recall_reply_never_written_is_wrong_on_every_question(self) -> None:
        recall = _row(kind="recall", energy={}, minutes=None, answered=False, words=None,
                      cut=None)
        assert meeting_words(recall) == "closed by vote; no reply, every answer wrong"

    def test_a_briefing_asks_for_no_answer(self) -> None:
        briefing = _row(kind="briefing", energy={}, minutes=None, answered=None, words=None,
                        cut=None)
        assert meeting_words(briefing) == "closed by vote"


def _progress(**changed: Any) -> Progress:
    progress: dict[str, Any] = {
        "packets": 3, "left": {"person-1": ("p1",), "person-2": (), "judge": ()},
        "written": (), "sheets": {}, "judge_cap_reached": False, "due": None,
    }
    return Progress(**{**progress, **changed})


class TestProgress:
    def test_each_rater_says_what_is_left(self) -> None:
        words = progress_words(_progress())
        assert "  person-1: 1 left (p1)\n" in words
        assert "  judge: every score in\n" in words
        assert words.endswith("The seal opens only once every score is in\n")

    def test_a_judge_its_cap_stopped_has_not_scored_every_packet(self) -> None:
        words = progress_words(_progress(
            left={"person-1": (), "person-2": (), "judge": ()},
            judge_cap_reached=True, never_judged=("p2", "p3"),
        ))
        assert "  judge: stopped by its cap, 2 packets never judged: the result will be " \
               "inconclusive\n" in words
        assert "judge: every score in" not in words
        assert words.endswith("Scoring is over: open the seal with --open\n")
