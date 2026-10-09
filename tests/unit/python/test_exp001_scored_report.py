"""EXP-001 harness — the scored run's report (PR 6c).

The scored run's report says how the run ended and why, by the rules of
pre-registration §3 and part 2 §6: complete, incomplete (the $150 cap, fewer
than four series kept, a third harness fault), or stopped by a harness fault
until its fix merges. It gives what the decision reads and the operator
needs before scoring: the series every arm's comparisons keep, each arm's
dollars per plan, real spend against the cap, the judge's spend, and the
date scoring is due. It never reads the seal, so it names no arm's scores.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from typing import Any

import pytest

from evaluators.exp001.attempts import Held, SeriesRun, Try
from evaluators.exp001.costs import ARMS_MODEL, CallPurpose, CallRecord
from evaluators.exp001.deployment import ARMS_ALIAS
from evaluators.exp001.judge import JUDGE_MODEL, Judged
from evaluators.exp001.materials import MeetingKind
from evaluators.exp001.pairs import Kept
from evaluators.exp001.runtime import CallLog
from evaluators.exp001.scored_report import build, outcome, summary

from ._exp001_run_test_helpers import SCORED

S1 = SCORED[0]
_T0 = dt.datetime(2036, 10, 6, 10, 0, tzinfo=dt.UTC)
_ALL = [s.id for s in SCORED]


def _window(
    number: int = 1, *, kept: list[str] | None = None, dropped: list[str] | None = None,
    stopped_by: str | None = None, fault: str | None = None, ended: bool = True,
    fixed_by: str | None = None,
) -> dict[str, Any]:
    kept = _ALL if kept is None else kept
    dropped = dropped or []
    return {
        "window": number, "fixed_by": fixed_by, "opened_at": _T0.isoformat(),
        "fault": None if fault is None else {"message": fault, "at": _T0.isoformat()},
        "ended": None if not ended else {
            "at": (_T0 + dt.timedelta(days=2)).isoformat(),
            "last_scored_meeting_at": (_T0 + dt.timedelta(days=1)).isoformat(),
            "kept": kept, "dropped": dropped,
            "not_held": [s for s in _ALL if s not in kept and s not in dropped],
            "stopped_by": stopped_by,
        },
    }


def _call(
    meeting: str, purpose: CallPurpose = CallPurpose.REPLY, *, arm: str = "C", attempt: int = 1,
    meeting_try: int = 1, output: int = 100_000, kind: MeetingKind = MeetingKind.PLAN,
) -> CallRecord:
    """A call of *output* output tokens: $1.50 on the arms' model."""
    return CallRecord(
        arm=arm, series=S1.id, meeting=meeting, meeting_kind=kind, attempt=attempt,
        adviser=None if arm == "A" else "ripple-kite", purpose=purpose, started_at=_T0,
        model=ARMS_MODEL, input_tokens=0, output_tokens=output, meeting_try=meeting_try,
    )


def _finished(arm: str, *, cut_short: str | None = None) -> SeriesRun[Kept]:
    """*arm*'s series 1, held to the end in its first attempt; the meeting
    *cut_short* at its first try is held again as its second."""
    tries: list[Try[Kept]] = []
    for meeting in S1.meetings:
        if meeting.id == cut_short:
            tries.append(Try(meeting.id, 1, 1, Held(None, errors=("RateLimitError",))))
        record: Mapping[str, Any] = {"closed_by": "vote", "failures": []}
        number = 2 if meeting.id == cut_short else 1
        tries.append(Try(meeting.id, 1, number, Held(Kept("The memo.", record))))
    return SeriesRun(arm, S1.id, tuple(tries), 1)


def _calls() -> dict[tuple[str, str], CallLog]:
    """Arm C's calls in series 1, whose first plan meeting was cut short once."""
    return {(S1.id, "C"): CallLog((
        _call(S1.meetings[0].id, kind=MeetingKind.BRIEFING),
        _call(S1.meetings[1].id),  # cut short: real spend, not the arm's
        _call(S1.meetings[1].id, meeting_try=2),
        _call(S1.meetings[2].id, CallPurpose.BID, output=10_000),
        _call(S1.meetings[5].id, kind=MeetingKind.RECALL),  # the recall check counts in no plan
    ), ())}


def _report(**overrides: Any) -> dict[str, Any]:
    runs = [_finished("C", cut_short=S1.meetings[1].id)]
    calls = _calls()
    arguments: dict[str, Any] = {
        "alias": ARMS_ALIAS, "windows": [_window()], "runs": runs, "calls": calls,
        "series": SCORED, "priced": True, "spend": [7.65],
        "everything": CallLog(calls[(S1.id, "C")].records, ()), "unread": [],
        "judge_calls": None, "judged": None,
    }
    arguments.update(overrides)
    return build(**arguments)


class TestTheOutcome:
    def test_a_window_whose_meetings_kept_four_series_or_more_is_complete(self) -> None:
        assert outcome([_window(kept=_ALL[1:], dropped=_ALL[:1])]) == ("complete", None)

    def test_the_cap_leaves_it_incomplete(self) -> None:
        assert outcome([_window(stopped_by="cap")]) == ("incomplete", "the $150 cap was reached")

    def test_fewer_than_four_series_kept_leave_it_incomplete(self) -> None:
        """However the series were lost: dropped, or not held before the window closed."""
        window = _window(kept=_ALL[:3], stopped_by="window")
        assert outcome([window]) == ("incomplete", "fewer than four series were kept")

    def test_a_third_harness_fault_leaves_it_incomplete(self) -> None:
        windows = [_window(n, fault="bad", ended=False) for n in (1, 2, 3)]
        assert outcome(windows) == ("incomplete", "a third harness fault")

    def test_a_fault_stops_it_until_its_fix_merges(self) -> None:
        state, why = outcome([_window(fault="bad", ended=False)])
        assert state == "stopped"
        assert why is not None and "window 2" in why and "--fixed-by" in why


class TestTheReport:
    def test_dollars_per_plan_count_each_meetings_finished_try_and_no_recall_check(
        self,
    ) -> None:
        """The briefing, the plan's second try and the bid: $1.50 + $1.50 + $0.15."""
        report = _report()
        assert report["dollars_per_plan"] == {"C": {S1.id: pytest.approx(3.15 / 4)}}

    def test_repriced_the_bids_move_to_the_fast_model(self) -> None:
        report = _report()
        bid = 10_000 * 5.00 / 1_000_000
        assert report["dollars_per_plan_repriced"] == {
            "C": {S1.id: pytest.approx((3.00 + bid) / 4)},
        }

    def test_a_dropped_series_has_no_dollars(self) -> None:
        dropped: SeriesRun[Kept] = SeriesRun("D", S1.id, (), None)
        report = _report(runs=[_finished("C", cut_short=S1.meetings[1].id), dropped],
                         windows=[_window(kept=_ALL[1:], dropped=_ALL[:1])])
        assert "D" not in report["dollars_per_plan"]

    def test_offline_nothing_is_priced(self) -> None:
        report = _report(priced=False, spend=[None])
        assert report["dollars_per_plan"] is None and report["real_spend"]["dollars"] is None

    def test_real_spend_counts_every_window_against_the_cap(self) -> None:
        windows = [_window(1, fault="bad", ended=False), _window(2, fixed_by="#2001")]
        report = _report(windows=windows, spend=[12.5, 40.0])
        assert report["real_spend"]["dollars"] == pytest.approx(52.5)
        assert report["real_spend"]["cap"] == 150.0
        assert [w["spend"] for w in report["windows"]] == [12.5, 40.0]

    def test_each_meeting_is_listed_with_its_series(self) -> None:
        rows = _report()["meetings"]
        assert [(r["series"], r["arm"], r["meeting"], r["try"]) for r in rows][:3] == [
            (S1.id, "C", S1.meetings[0].id, 1), (S1.id, "C", S1.meetings[1].id, 1),
            (S1.id, "C", S1.meetings[1].id, 2),
        ]

    def test_scoring_is_due_21_days_after_the_last_scored_meeting(self) -> None:
        """Not after the window's last try: series 5 may have been begun and never kept."""
        ended = _T0 + dt.timedelta(days=1)
        assert _report()["scoring_due"] == {
            "last_meeting_ended_at": ended.isoformat(),
            "by": (ended + dt.timedelta(days=21)).isoformat(),
        }

    def test_the_tries_of_a_pair_the_cap_or_window_stopped_are_listed_with_no_dollars(
        self,
    ) -> None:
        stopped: SeriesRun[Kept] = SeriesRun("D", S1.id, _finished("D").tries[:2], None)
        report = _report(stopped=[stopped], windows=[_window(kept=_ALL[1:], stopped_by="window")])
        rows = [(r["arm"], r["meeting"]) for r in report["meetings"]]
        assert rows[-2:] == [("D", S1.meetings[0].id), ("D", S1.meetings[1].id)]
        assert "D" not in report["dollars_per_plan"]

    def test_calls_the_fixed_table_cannot_price_are_named_and_left_out(self) -> None:
        unpriced = [{"window": 1, "model": "claude-unlisted-1", "calls": 2}]
        windows = [_window(1, fault="bad", ended=False), _window(2, fixed_by="#2001")]
        report = _report(windows=windows, spend=[0.5, 40.0], unpriced=unpriced)
        assert report["real_spend"]["unpriced"] == unpriced
        assert report["real_spend"]["dollars"] == pytest.approx(40.5)
        assert ("left out of real spend, as the fixed table cannot price them: window 1, "
                "2 calls on claude-unlisted-1") in summary(report)

    def test_a_judge_log_the_harness_cannot_read_is_named_in_the_usage_alone(self) -> None:
        named = "window-1/judging/scored/calls.jsonl"
        report = _report(unread=["window-1/pairs/x/calls.jsonl"], judge_unread=[named])
        assert report["usage"]["unread"] == ["window-1/pairs/x/calls.jsonl", named]
        assert report["real_spend"]["unread"] == ["window-1/pairs/x/calls.jsonl"]
        assert named in summary(report)

    def test_judging_a_provider_error_stopped_is_said_and_resumed_by_a_start(self) -> None:
        report = _report(judging_stopped="RateLimitError: overloaded")
        assert report["judging_stopped"] == "RateLimitError: overloaded"
        assert ("The judge: stopped by a provider error its retries did not clear "
                "(RateLimitError: overloaded); start the run again to resume it") in summary(report)
        assert _report()["judging_stopped"] is None

    def test_an_incomplete_run_is_not_scored(self) -> None:
        report = _report(windows=[_window(stopped_by="cap")])
        assert report["scoring_due"] is None

    def test_a_stopped_window_shows_no_series_and_no_meetings(self) -> None:
        """Its outputs are discarded: what counts is the next window's."""
        report = _report(windows=[_window(fault="bad", ended=False)], runs=[], calls={})
        assert (report["series"], report["meetings"], report["dollars_per_plan"]) == (
            None, [], None,
        )

    def test_the_judge_is_reported_by_its_spend_never_its_scores(self) -> None:
        judge_calls = CallLog((CallRecord(
            arm="", series="scored", meeting="ab12cd34", meeting_kind=MeetingKind.PLAN,
            attempt=1, adviser=None, purpose=CallPurpose.JUDGE, started_at=_T0,
            model=JUDGE_MODEL, input_tokens=3000, output_tokens=900,
        ),), ())
        judged = Judged({}, {}, spend=0.0375, cap_reached=False, left=())
        arms = _report()["usage"]["models"][ARMS_MODEL]["calls"]
        everything = CallLog((*_calls()[(S1.id, "C")].records, *judge_calls.records), ())
        report = _report(judge_calls=judge_calls, judged=judged, everything=everything)
        assert report["judging"] == {
            "calls": 1, "failed_calls": 0, "spend": 0.0375, "cap": 25.0, "cap_reached": False,
            "left": [], "output_tokens_max": 900,
        }
        assert "recall_marks" not in report
        assert report["usage"]["models"][JUDGE_MODEL]["calls"] == 1
        assert report["usage"]["models"][ARMS_MODEL]["calls"] == arms

    def test_the_summary_names_what_the_run_shows(self) -> None:
        windows = [_window(1, fault="the call log is unreadable", ended=False),
                   _window(2, fixed_by="#2001")]
        text = summary(_report(windows=windows, spend=[12.5, 40.0]))
        assert text.startswith("EXP-001 scored run on anthropic claude-sonnet-4-6: complete")
        assert "window 1: stopped by a harness fault: the call log is unreadable" in text
        assert "window 2, after #2001" in text
        assert "$52.50 of the $150 cap" in text
        assert f"kept: {', '.join(_ALL)}" in text
        assert "C: series-1 $0.7875" in text
        assert "scoring is due by" in text
        assert ARMS_MODEL in text
