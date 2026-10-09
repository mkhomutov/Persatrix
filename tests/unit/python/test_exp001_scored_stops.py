"""EXP-001 harness — what the scored run records when it stops (PR 6c).

A scored run can stop on a harness fault, at its cap, when its window
closes, on a provider error the judge's retries do not clear, or on a
crash, and it is started again in the same directory. Whatever stopped it,
the report is written, and nothing a closed window left behind stops a later
one. Holding the run itself is in ``test_exp001_scored.py``.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest

from agents import call_log
from evaluators.exp001.attempts import HarnessFault
from evaluators.exp001.costs import arm_orders
from evaluators.exp001.deployed_meeting import CALL_LOG
from evaluators.exp001.deployment import ARMS_ALIAS
from evaluators.exp001.judge import JudgeFault
from evaluators.exp001.scored import (
    BATCH,
    JUDGING,
    REPORT,
    RUN,
    JudgingStopped,
    RefusedError,
    window_directory,
)

from ._exp001_run_test_helpers import SCORED, T0
from ._exp001_scored_test_helpers import (
    _EVERY_TRY,
    _Clock,
    _Crash,
    _Holds,
    _Judge,
    _scored,
    _state,
)

# A model the fixed table does not list, so no call on it can be priced.
_UNLISTED = "claude-unlisted-1"
_HOUR = dt.timedelta(hours=1)


@pytest.fixture(autouse=True)
def _no_ambient_log(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(call_log.CALL_LOG_ENV, raising=False)
    call_log.reset_call_log()


class RateLimitError(Exception):
    """Named as the provider's SDK names a rate limit, so the judge retries it."""


def _report(root: Path) -> dict[str, Any]:
    return dict(json.loads((root / REPORT).read_text()))


def _last_try() -> tuple[str, str, str, int, int]:
    """The window's last try: series 5's recall check, in its last arm."""
    return (arm_orders(5)[-1][-1], "series-5", SCORED[-1].meetings[-1].id, 1, 1)


class TestUnpricedCalls:
    """The $150 cap row: a call on a model the fixed table does not list
    stops the run as a harness fault, since PR 1's row refuses to price it
    at zero."""

    _AT = ("A", "series-1", "series-1-plan-1", 1, 1)

    async def test_the_check_before_the_next_try_finds_it_and_closes_the_window(
        self, tmp_path: Path,
    ) -> None:
        holds = _Holds(models={self._AT: _UNLISTED})
        with pytest.raises(HarnessFault, match=_UNLISTED):
            await _scored(tmp_path, holds, _Judge())
        assert len(holds.held) == 2  # the briefing and plan 1; plan 2 is not begun
        (window,) = _state(tmp_path)["windows"]
        assert _UNLISTED in window["fault"]["message"]
        assert _report(tmp_path)["outcome"] == "stopped"

    async def test_once_its_window_is_closed_they_are_named_and_stop_no_later_window(
        self, tmp_path: Path,
    ) -> None:
        """The fix cannot price calls already made: the closed window's are
        left out of real spend and named, as a log it cannot read is."""
        with pytest.raises(HarnessFault):
            await _scored(tmp_path, _Holds(models={self._AT: _UNLISTED}), _Judge())
        holds = _Holds()
        report = await _scored(tmp_path, holds, _Judge(), fixed_by="#2004")
        assert len(holds.held) == _EVERY_TRY
        assert report["outcome"] == "complete"
        assert report["real_spend"]["unpriced"] == [
            {"window": 1, "model": _UNLISTED, "calls": 1},
        ]
        assert [w["spend"] for w in report["windows"]] == pytest.approx([0.15, 22.5])

    async def test_one_in_the_window_s_last_try_is_found_before_anything_is_judged(
        self, tmp_path: Path,
    ) -> None:
        judge = _Judge()
        with pytest.raises(HarnessFault, match=_UNLISTED):
            await _scored(tmp_path, _Holds(models={_last_try(): _UNLISTED}), judge)
        assert judge.asked == []
        (window,) = _state(tmp_path)["windows"]
        assert window["fault"] is not None and window["ended"] is None
        assert _report(tmp_path)["outcome"] == "stopped"


class TestTheCapAtTheEnd:
    async def test_a_last_try_that_crosses_it_stops_nothing_so_the_run_is_complete(
        self, tmp_path: Path,
    ) -> None:
        """Pre-registration §3: a run stopped by its cap is incomplete. At
        about $1.003 a try only the 150th crosses $150, and no try is left
        for the cap to refuse."""
        holds = _Holds(tokens=66_867)
        report = await _scored(tmp_path, holds, _Judge())
        assert len(holds.held) == _EVERY_TRY
        assert report["real_spend"]["dollars"] == pytest.approx(150.45, abs=0.01)
        assert (report["outcome"], report["windows"][0]["ended"]["stopped_by"]) == (
            "complete", None,
        )


class TestWhenScoringIsDue:
    """Part 2 §4: scoring is due within 21 days of the last scored meeting."""

    async def test_a_window_found_closed_on_a_later_start_counts_from_its_last_try(
        self, tmp_path: Path,
    ) -> None:
        """Series 1 to 4 end with the 120th hourly try. A crash at series 5's
        first try is started again on day 9, when the seven days are over."""
        clock = _Clock()
        crash = {(arm_orders(5)[4][0], "series-5", SCORED[4].meetings[0].id, 1, 1): _Crash()}
        with pytest.raises(_Crash):
            await _scored(tmp_path, _Holds(clock=clock, step=_HOUR, acts=crash), _Judge(),
                          clock=clock)
        clock.at = T0 + dt.timedelta(days=9)
        report = await _scored(tmp_path, _Holds(clock=clock, step=_HOUR), _Judge(), clock=clock)
        ended = T0 + 120 * _HOUR
        assert report["series"]["kept"] == [s.id for s in SCORED[:4]]
        assert report["windows"][0]["ended"]["at"] == ended.isoformat()
        assert report["scoring_due"] == {
            "last_meeting_ended_at": ended.isoformat(),
            "by": (ended + dt.timedelta(days=21)).isoformat(),
        }

    async def test_a_series_not_held_in_every_arm_holds_no_scored_meeting(
        self, tmp_path: Path,
    ) -> None:
        """At 72 minutes a try, series 4 ends at six days, and series 5's 20
        tries run on until the seven days are over."""
        clock, step = _Clock(), dt.timedelta(minutes=72)
        report = await _scored(tmp_path, _Holds(clock=clock, step=step), _Judge(), clock=clock)
        assert report["windows"][0]["ended"]["at"] == (T0 + 140 * step).isoformat()
        assert report["scoring_due"]["last_meeting_ended_at"] == (T0 + 120 * step).isoformat()


class TestJudgeLogs:
    async def test_one_the_harness_cannot_read_is_named_and_every_report_still_written(
        self, tmp_path: Path,
    ) -> None:
        log = window_directory(tmp_path, 1) / JUDGING / BATCH / CALL_LOG
        with pytest.raises(JudgeFault, match="cannot be read"):
            await _scored(tmp_path, _Holds(), _Judge(corrupt=log))
        named = str(log.relative_to(tmp_path))
        stopped = _report(tmp_path)
        assert stopped["outcome"] == "stopped"
        assert named in stopped["usage"]["unread"]
        report = await _scored(tmp_path, _Holds(), _Judge(), fixed_by="#2005")
        assert report["outcome"] == "complete"
        assert named in report["usage"]["unread"]
        assert named not in report["real_spend"]["unread"]  # no arm call is in it


class TestJudgingStopped:
    """The scored judging row: a provider error the judge's retries do not
    clear stops the run, and a later start resumes the batch."""

    async def test_the_run_stops_with_its_report_written_and_no_fault(
        self, tmp_path: Path,
    ) -> None:
        with pytest.raises(JudgingStopped, match="RateLimitError"):
            await _scored(tmp_path, _Holds(), _Judge(error=RateLimitError))
        written = _report(tmp_path)
        assert "RateLimitError" in str(written["judging_stopped"])
        assert written["judging"] is None
        assert _state(tmp_path)["windows"][0]["fault"] is None

    async def test_a_later_start_resumes_the_batch(self, tmp_path: Path) -> None:
        with pytest.raises(JudgingStopped):
            await _scored(tmp_path, _Holds(), _Judge(error=RateLimitError))
        holds, judge = _Holds(), _Judge()
        report = await _scored(tmp_path, holds, judge)
        assert holds.held == []
        assert len(judge.asked) == 125
        assert report["judging_stopped"] is None and report["judging"] is not None


class TestAStoppedPair:
    async def test_the_tries_the_cap_interrupted_are_reported_and_never_priced_per_plan(
        self, tmp_path: Path,
    ) -> None:
        """At $15 a try the cap refuses C's fifth meeting of series 1, after A's six."""
        report = await _scored(tmp_path, _Holds(tokens=1_000_000), _Judge())
        assert [(row["arm"], row["meeting"]) for row in report["meetings"]] == [
            ("A", m.id) for m in SCORED[0].meetings
        ] + [("C", m.id) for m in SCORED[0].meetings[:4]]
        assert list(report["dollars_per_plan"]) == ["A"]


class TestAnotherRunsDirectory:
    async def test_a_practice_run_s_directory_is_refused(self, tmp_path: Path) -> None:
        practice = {"arms": ["A"], "alias": dataclasses.asdict(ARMS_ALIAS)}
        (tmp_path / RUN).write_text(json.dumps(practice))
        holds = _Holds()
        with pytest.raises(RefusedError, match="practice run"):
            await _scored(tmp_path, holds, _Judge())
        assert holds.held == []
        assert json.loads((tmp_path / RUN).read_text()) == practice
