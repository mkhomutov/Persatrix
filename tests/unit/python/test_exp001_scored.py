"""EXP-001 harness — the scored run, start to finish (PR 6c).

Pre-registration §3: the scored series run in order, 1 to 5, each series'
five arms one at a time in the order drawn for it, and every scored meeting
within seven days of the first. A harness fault stops the run and discards
every scored output so far: after the fix the series are held again from
series 1, in a new window whose seven days start again, and a third fault
ends the run, incomplete. Every scored attempt counts toward the $150 cap,
discarded or not. Once a window's meetings are held, the answers of the
series it kept are drawn into packets and the judge scores them as batch
``scored``. What the report holds is in ``test_exp001_scored_report.py``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from agents import call_log
from agents.llm_client import LLMClient, LLMResponse, Usage
from agents.llm_types import LLMToolResult
from evaluators.exp001.attempts import HarnessFault, Held, Hold, try_directory
from evaluators.exp001.costs import ARMS, ARMS_MODEL, arm_orders
from evaluators.exp001.deployed_meeting import CALL_LOG
from evaluators.exp001.deployment import ARMS_ALIAS, Alias
from evaluators.exp001.judge import JudgeFault, read_judge_log
from evaluators.exp001.materials import Meeting, Series
from evaluators.exp001.packets import load_adviser_names
from evaluators.exp001.rating import packet_text, read_packets
from evaluators.exp001.scored import (
    BATCH,
    INTERRUPTED,
    JUDGING,
    REPORT,
    RUN,
    SUMMARY,
    RefusedError,
    run_scored,
    window_directory,
)

from ._exp001_judge_test_helpers import PROMPTS, _memo_json
from ._exp001_run_test_helpers import EXP, PANEL, SCORED, T0, arm_a_reply, channel_meeting, no_wait

NAMES = load_adviser_names(EXP / "panel.yaml")
_BINARY = Path("/repo/bin/persatrix-server")
_OFFLINE = Alias("mock", "offline", 0, 0)
_EVERY_TRY = sum(len(s.meetings) for s in SCORED) * len(ARMS)  # 150, when none fails
_RATE_LIMITED: Held[Any] = Held(None, errors=("RateLimitError",))
# A try that logs this many output tokens on the arms' model costs $0.15.
_TOKENS = 10_000


@pytest.fixture(autouse=True)
def _no_ambient_log(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(call_log.CALL_LOG_ENV, raising=False)
    call_log.reset_call_log()


class _Crash(BaseException):
    """Stands in for Ctrl-C, a hangup or a crash: no Exception, so no fault."""


class _Clock:
    """The harness's real time, moved on only by the holds."""

    def __init__(self) -> None:
        self.at = T0

    def __call__(self) -> dt.datetime:
        return self.at


class _Judge:
    """Answers every packet readably, as the judge would, and keeps what it was asked."""

    name = "anthropic"
    supports_prompt_cache = True

    def __init__(self, *, readable: bool = True) -> None:
        self.asked: list[str] = []
        self.readable = readable

    async def create_message(self, **kwargs: Any) -> LLMResponse:
        packet = kwargs["messages"][0]["content"]
        self.asked.append(packet)
        if not self.readable:
            answer = "I would rather not score this."
        elif kwargs["system"] == PROMPTS.recall:
            questions = re.findall(r"^(R\d+), question", packet, flags=re.MULTILINE)
            answer = json.dumps(dict.fromkeys(questions, "right"))
        else:
            answer = _memo_json(c2=None if "decision: none." in packet else 2)
        return LLMResponse(text=answer, usage=Usage(3000, 900))

    def format_tool_definitions(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return tools

    def append_tool_round(
        self, messages: list[Any], response: LLMResponse, tool_results: list[LLMToolResult],
    ) -> list[Any]:
        raise AssertionError("the judge sends no tools")


class _Holds:
    """Stands in for each arm's hold: every meeting held at its first try,
    each try logging one call of *tokens* output tokens on *model*. *acts*
    replaces the try at (arm, series, meeting, attempt, try), once it has
    logged its call, with a Held, or raises. Each try moves *clock* on by
    *step*. The try *paused* at waits until ``go_on`` is set."""

    def __init__(
        self, *, tokens: int = _TOKENS, model: str = ARMS_MODEL,
        acts: Mapping[tuple[str, str, str, int, int], Held[Any] | BaseException] | None = None,
        clock: _Clock | None = None, step: dt.timedelta = dt.timedelta(0),
        paused: tuple[str, str, str, int, int] | None = None,
    ) -> None:
        self.tokens, self.model, self.acts = tokens, model, dict(acts or {})
        self.clock, self.step, self.paused = clock, step, paused
        self.go_on = asyncio.Event()
        self.held: list[tuple[str, str, str]] = []

    def __call__(self, arm: str, series: Series, directory: Path) -> Hold[Any]:
        async def hold(meeting: Meeting, attempt: int, meeting_try: int) -> Held[Any]:
            key = (arm, series.id, meeting.id, attempt, meeting_try)
            if key == self.paused:
                await self.go_on.wait()
            self.held.append((series.id, arm, meeting.id))
            if self.clock is not None:
                self.clock.at += self.step
            self._log(arm, series, meeting, attempt, meeting_try, directory)
            act = self.acts.get(key)
            if isinstance(act, BaseException):
                raise act
            if act is not None:
                return act
            if arm == "A":
                return Held(arm_a_reply(meeting, f"A at {meeting.id}.", series=series))
            return Held(channel_meeting(meeting, attempt, meeting_try, arm=arm, series=series,
                                        memo=f"{arm}'s chair at {meeting.id}."))
        return hold

    def _log(
        self, arm: str, series: Series, meeting: Meeting, attempt: int, meeting_try: int,
        directory: Path,
    ) -> None:
        """One call, where the arm's own hold logs it: arm A's series to one
        file, each channel-arm try to its own."""
        path = directory / CALL_LOG if arm == "A" else (
            try_directory(directory, attempt, meeting, meeting_try) / CALL_LOG
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        line = {
            "tags": {"arm": arm, "series": series.id, "meeting": meeting.id,
                     "meeting_kind": meeting.kind.value, "attempt": str(attempt),
                     "try": str(meeting_try)},
            "agent_id": PANEL.chair.id, "purpose": "turn", "provider": "anthropic",
            "model": self.model, "model_alias": "quality", "started_at": T0.isoformat(),
            "input_tokens": 0, "output_tokens": self.tokens, "cache_write_tokens": 0,
            "cache_read_tokens": 0, "cache_prefix_sha256": None, "error": None,
        }
        with path.open("a") as log:
            log.write(json.dumps(line) + "\n")


def _failing(arm: str, series: str, meeting: str) -> dict[tuple[str, str, str, int, int], Any]:
    """*arm*'s *meeting* cut short at every try of both attempts: the series is dropped."""
    return {(arm, series, meeting, attempt, n): _RATE_LIMITED
            for attempt in (1, 2) for n in range(1, 5)}


async def _scored(
    root: Path, holds: _Holds, judge: _Judge | None, *, alias: Alias = ARMS_ALIAS,
    fixed_by: str | None = None, clock: _Clock | None = None,
    series: tuple[Series, ...] = SCORED,
) -> dict[str, Any]:
    return await run_scored(
        root, panel=PANEL, series=series, names=NAMES, client=LLMClient(judge or _Judge()),
        binary=_BINARY, alias=alias, prompts=None if judge is None else PROMPTS,
        fixed_by=fixed_by, sleep=no_wait, now=clock or _Clock(), make_hold=holds,
    )


def _state(root: Path) -> dict[str, Any]:
    return dict(json.loads((root / RUN).read_text()))


class TestAScoredRun:
    async def test_the_series_run_in_order_each_one_s_arms_in_its_drawn_order(
        self, tmp_path: Path,
    ) -> None:
        holds = _Holds()
        await _scored(tmp_path, holds, _Judge())
        pairs = list(dict.fromkeys((s, arm) for s, arm, _ in holds.held))
        assert pairs == [(s.id, arm) for s, order in zip(SCORED, arm_orders(5), strict=True)
                         for arm in order]
        assert len(holds.held) == _EVERY_TRY
        assert sorted(p.name for p in (window_directory(tmp_path, 1) / "pairs").iterdir()) == [
            s.id for s in SCORED
        ]

    async def test_the_kept_series_answers_are_drawn_and_judged_as_batch_scored(
        self, tmp_path: Path,
    ) -> None:
        judge = _Judge()
        report = await _scored(tmp_path, _Holds(), judge)
        window = window_directory(tmp_path, 1)
        drawn = read_packets(window)
        assert len(drawn.packets) == 125  # every arm's 20 memos and 5 recall checks
        assert judge.asked == [packet_text(p) for p in drawn.order("judge")]
        calls = read_judge_log(window / JUDGING / BATCH / CALL_LOG)
        assert {r.series for r in calls.records} == {BATCH} == {"scored"}
        assert (window / "raters" / "person-2.txt").is_file()
        assert report["outcome"] == "complete"

    async def test_offline_nothing_is_judged_or_priced(self, tmp_path: Path) -> None:
        """A rehearsal on the mock provider costs nothing, so the cap never stops it."""
        report = await _scored(tmp_path, _Holds(model="offline", tokens=10**9), None,
                               alias=_OFFLINE)
        assert report["outcome"] == "complete"
        assert report["judging"] is None and report["dollars_per_plan"] is None
        assert (window_directory(tmp_path, 1) / "raters" / "person-1.txt").is_file()

    async def test_only_the_five_scored_series_in_order_are_held(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="the five scored series"):
            await _scored(tmp_path, _Holds(), _Judge(), series=SCORED[::-1])


class TestDroppedSeries:
    async def test_a_series_dropped_in_one_arm_is_held_no_further_and_never_drawn(
        self, tmp_path: Path,
    ) -> None:
        """Series 1's order is A, C, D, D′, B: once D drops it, D′ and B never hold it."""
        holds = _Holds(acts=_failing("D", "series-1", "series-1-plan-1"))
        report = await _scored(tmp_path, holds, _Judge())
        assert {arm for s, arm, _ in holds.held if s == "series-1"} == {"A", "C", "D"}
        assert report["series"] == {
            "kept": ["series-2", "series-3", "series-4", "series-5"],
            "dropped": ["series-1"], "not_held": [],
        }
        assert report["outcome"] == "complete"
        assert len(read_packets(window_directory(tmp_path, 1)).packets) == 100

    async def test_once_fewer_than_four_series_can_be_kept_the_run_stops(
        self, tmp_path: Path,
    ) -> None:
        """Series 2's order starts with D′, so nothing more of it is held."""
        acts = {**_failing("D", "series-1", "series-1-plan-1"),
                **_failing("D-prime", "series-2", "series-2-plan-1")}
        holds, judge = _Holds(acts=acts), _Judge()
        report = await _scored(tmp_path, holds, judge)
        assert {s for s, _, _ in holds.held} == {"series-1", "series-2"}
        assert (report["outcome"], report["why"]) == (
            "incomplete", "fewer than four series were kept",
        )
        assert judge.asked == []
        assert not (window_directory(tmp_path, 1) / "packets.json").exists()


class TestHarnessFaults:
    _AT = ("C", "series-1", "series-1-plan-2", 1, 1)

    async def test_a_fault_stops_the_run_and_closes_its_window_whole(
        self, tmp_path: Path,
    ) -> None:
        holds = _Holds(acts={self._AT: HarnessFault("the call log is unreadable")})
        with pytest.raises(HarnessFault, match="unreadable"):
            await _scored(tmp_path, holds, _Judge())
        (window,) = _state(tmp_path)["windows"]
        assert "the call log is unreadable" in window["fault"]["message"]
        # Its outputs stay as they were, set aside and published with the result.
        assert (window_directory(tmp_path, 1) / "pairs" / "series-1" / "A" / "pair.json").is_file()

    async def test_after_a_fault_the_next_window_starts_only_once_told_the_fix(
        self, tmp_path: Path,
    ) -> None:
        with pytest.raises(HarnessFault):
            await _scored(tmp_path, _Holds(acts={self._AT: HarnessFault("bad")}), _Judge())
        holds = _Holds()
        with pytest.raises(RefusedError, match="--fixed-by"):
            await _scored(tmp_path, holds, _Judge())
        assert holds.held == []
        report = await _scored(tmp_path, holds, _Judge(), fixed_by="#2001")
        assert holds.held[0] == ("series-1", "A", "series-1-briefing")
        assert len(holds.held) == _EVERY_TRY
        assert [(w["window"], w["fixed_by"]) for w in report["windows"]] == [
            (1, None), (2, "#2001"),
        ]
        assert report["outcome"] == "complete"
        assert not (window_directory(tmp_path, 1) / "packets.json").exists()
        assert len(read_packets(window_directory(tmp_path, 2)).packets) == 125

    async def test_the_seven_days_start_again_with_the_next_window(self, tmp_path: Path) -> None:
        clock = _Clock()
        with pytest.raises(HarnessFault):
            await _scored(tmp_path, _Holds(acts={self._AT: HarnessFault("bad")}), _Judge(),
                          clock=clock)
        clock.at += dt.timedelta(days=6)
        await _scored(tmp_path, _Holds(), _Judge(), fixed_by="#2001", clock=clock)
        assert [w["opened_at"] for w in _state(tmp_path)["windows"]] == [
            T0.isoformat(), (T0 + dt.timedelta(days=6)).isoformat(),
        ]

    async def test_a_fault_while_judging_discards_the_window_too(self, tmp_path: Path) -> None:
        """Judge choices: in the scored judging, an unreadable answer is a
        harness fault under pre-registration §3."""
        with pytest.raises(JudgeFault):
            await _scored(tmp_path, _Holds(), _Judge(readable=False))
        assert _state(tmp_path)["windows"][0]["fault"] is not None
        holds = _Holds()
        report = await _scored(tmp_path, holds, _Judge(), fixed_by="#2002")
        assert len(holds.held) == _EVERY_TRY
        assert report["outcome"] == "complete"

    async def test_a_third_fault_ends_the_run_incomplete(self, tmp_path: Path) -> None:
        first = ("A", "series-1", "series-1-briefing", 1, 1)
        for fixed_by in (None, "#1", "#2"):
            with pytest.raises(HarnessFault):
                await _scored(tmp_path, _Holds(acts={first: HarnessFault("bad")}), _Judge(),
                              fixed_by=fixed_by)
        assert json.loads((tmp_path / REPORT).read_text())["why"] == "a third harness fault"
        holds = _Holds()
        report = await _scored(tmp_path, holds, _Judge())
        assert holds.held == []
        assert (report["outcome"], report["why"]) == ("incomplete", "a third harness fault")
        with pytest.raises(RefusedError, match="third harness fault"):
            await _scored(tmp_path, holds, _Judge(), fixed_by="#3")

    async def test_naming_a_fix_with_no_fault_to_fix_is_refused(self, tmp_path: Path) -> None:
        holds = _Holds()
        with pytest.raises(RefusedError, match="no harness fault"):
            await _scored(tmp_path, holds, _Judge(), fixed_by="#9")
        assert holds.held == []

    async def test_a_fix_named_blank_is_no_fix(self, tmp_path: Path) -> None:
        with pytest.raises(HarnessFault):
            await _scored(tmp_path, _Holds(acts={self._AT: HarnessFault("bad")}), _Judge())
        holds = _Holds()
        with pytest.raises(RefusedError, match="--fixed-by"):
            await _scored(tmp_path, holds, _Judge(), fixed_by="  ")
        assert holds.held == [] and len(_state(tmp_path)["windows"]) == 1


class TestTheCap:
    async def test_no_try_begins_once_real_spend_reaches_150(self, tmp_path: Path) -> None:
        """At $15 a try the tenth reaches the cap, so it is the last."""
        holds, judge = _Holds(tokens=1_000_000), _Judge()
        report = await _scored(tmp_path, holds, judge)
        assert len(holds.held) == 10
        assert (report["outcome"], report["why"]) == ("incomplete", "the $150 cap was reached")
        assert report["real_spend"]["dollars"] == pytest.approx(150)
        assert judge.asked == []

    async def test_a_discarded_window_counts_toward_it(self, tmp_path: Path) -> None:
        """Window 1 spends $75, the fifth try's call included, before its fault."""
        fault = {("A", "series-1", "series-1-plan-4", 1, 1): HarnessFault("bad")}
        with pytest.raises(HarnessFault):
            await _scored(tmp_path, _Holds(tokens=1_000_000, acts=fault), _Judge())
        holds = _Holds(tokens=1_000_000)
        report = await _scored(tmp_path, holds, _Judge(), fixed_by="#2003")
        assert len(holds.held) == 5
        assert [w["spend"] for w in report["windows"]] == pytest.approx([75, 75])

    async def test_a_pair_set_aside_counts_toward_it(self, tmp_path: Path) -> None:
        crash = {("A", "series-1", "series-1-plan-2", 1, 1): _Crash()}
        with pytest.raises(_Crash):
            await _scored(tmp_path, _Holds(tokens=1_000_000, acts=crash), _Judge())
        holds = _Holds(tokens=1_000_000)
        await _scored(tmp_path, holds, _Judge())
        assert len(holds.held) == 7  # $45 set aside, and seven tries more
        assert (window_directory(tmp_path, 1) / INTERRUPTED / "series-1-A-1").is_dir()


class TestTheSevenDayWindow:
    async def test_it_opens_as_its_first_try_begins_and_says_when_it_closes(
        self, tmp_path: Path,
    ) -> None:
        clock, said = _Clock(), []
        await run_scored(
            tmp_path, panel=PANEL, series=SCORED, names=NAMES, client=LLMClient(_Judge()),
            binary=_BINARY, prompts=PROMPTS, sleep=no_wait, now=clock, progress=said.append,
            make_hold=_Holds(clock=clock, step=dt.timedelta(minutes=1)),
        )
        assert _state(tmp_path)["windows"][0]["opened_at"] == T0.isoformat()
        closes = (T0 + dt.timedelta(days=7)).isoformat()
        assert f"window 1 opens: no try begins at or after {closes}" in said

    async def test_no_try_begins_seven_days_after_the_first(self, tmp_path: Path) -> None:
        """At 72 minutes a try, the 141st would begin at seven days: series 5
        is not held in every arm, and the four before it are kept."""
        clock = _Clock()
        holds = _Holds(clock=clock, step=dt.timedelta(minutes=72))
        report = await _scored(tmp_path, holds, _Judge(), clock=clock)
        assert len(holds.held) == 140
        assert report["series"] == {
            "kept": ["series-1", "series-2", "series-3", "series-4"], "dropped": [],
            "not_held": ["series-5"],
        }
        assert report["outcome"] == "complete"
        assert report["windows"][0]["ended"]["stopped_by"] == "window"
        assert len(read_packets(window_directory(tmp_path, 1)).packets) == 100

    async def test_with_fewer_than_four_series_held_in_it_the_run_is_incomplete(
        self, tmp_path: Path,
    ) -> None:
        clock = _Clock()
        holds = _Holds(clock=clock, step=dt.timedelta(hours=6))
        report = await _scored(tmp_path, holds, _Judge(), clock=clock)
        assert len(holds.held) == 28
        assert (report["outcome"], report["why"]) == (
            "incomplete", "fewer than four series were kept",
        )


class TestStartedAgain:
    async def test_it_goes_on_where_it_stopped_and_asks_nothing_twice(
        self, tmp_path: Path,
    ) -> None:
        first = await _scored(tmp_path, _Holds(), _Judge())
        holds, judge = _Holds(), _Judge()
        again = await _scored(tmp_path, holds, judge)
        assert (holds.held, judge.asked) == ([], [])
        assert again == first

    async def test_a_pair_stopped_partway_is_held_again_in_the_same_window(
        self, tmp_path: Path,
    ) -> None:
        clock = _Clock()
        crash = {("C", "series-1", "series-1-plan-1", 1, 1): _Crash()}
        with pytest.raises(_Crash):
            await _scored(tmp_path, _Holds(acts=crash), _Judge(), clock=clock)
        clock.at += dt.timedelta(days=1)
        holds = _Holds()
        report = await _scored(tmp_path, holds, _Judge(), clock=clock)
        assert holds.held[0] == ("series-1", "C", "series-1-briefing")  # A's pair is read back
        assert [w["opened_at"] for w in report["windows"]] == [T0.isoformat()]
        assert (window_directory(tmp_path, 1) / INTERRUPTED / "series-1-C-1").is_dir()

    async def test_a_run_started_again_on_another_provider_is_refused(
        self, tmp_path: Path,
    ) -> None:
        await _scored(tmp_path, _Holds(model="offline"), None, alias=_OFFLINE)
        holds = _Holds()
        with pytest.raises(RefusedError, match="mock offline, not anthropic claude-sonnet-4-6"):
            await _scored(tmp_path, holds, _Judge())
        assert holds.held == []

    async def test_a_second_run_at_once_is_refused(self, tmp_path: Path) -> None:
        holds = _Holds(paused=("A", "series-1", "series-1-plan-1", 1, 1))
        first = asyncio.create_task(_scored(tmp_path, holds, _Judge()))
        await asyncio.sleep(0)  # the first run holds A's first plan, paused
        second = _Holds()
        with pytest.raises(RefusedError, match="another run is holding this scored run"):
            await _scored(tmp_path, second, _Judge())
        assert second.held == []
        holds.go_on.set()
        await first

    async def test_the_report_is_written_and_returned(self, tmp_path: Path) -> None:
        report = await _scored(tmp_path, _Holds(), _Judge())
        assert json.loads((tmp_path / REPORT).read_text()) == json.loads(json.dumps(report))
        assert (tmp_path / SUMMARY).read_text().startswith("EXP-001 scored run")
