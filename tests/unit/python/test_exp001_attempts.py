"""EXP-001 harness — attempts and failures, for every arm (PR 5b).

Pre-registration §3 sorts what can go wrong at a meeting into three kinds.
A provider error is retried up to three times, by holding the meeting again;
a series whose meeting still fails starts again from its briefing, once, and
is then dropped from every arm's comparisons. A failure the system causes is
recorded and the meeting stands. A harness fault stops the run.
"""

from __future__ import annotations

import builtins
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import anthropic
import pytest

from agents.llm_client import LLMClient, LLMResponse, Usage
from agents.llm_types import LLMToolResult
from evaluators.exp001.arm_a import ArmAReply
from evaluators.exp001.attempts import (
    MIN_SERIES,
    PROVIDER_ERRORS,
    RETRIES,
    SYSTEM_ERRORS,
    ErrorKind,
    HarnessFault,
    Held,
    SeriesRun,
    arm_a_hold,
    channel_hold,
    error_kind,
    run_series,
    series_kept,
    try_directory,
)
from evaluators.exp001.channel_arm import CALL_LOG
from evaluators.exp001.deployment import DeploymentError, StartError
from evaluators.exp001.materials import Meeting, load_series
from evaluators.exp001.orchestrator import OrchestratorError
from evaluators.exp001.panel import load_panel
from evaluators.exp001.runtime import read_call_log

_EXP = Path(__file__).resolve().parents[3] / "evaluators" / "experiments" / "EXP-001"
PANEL = load_panel(_EXP / "panel.yaml")
SERIES = load_series(_EXP / "practice.yaml")
IDS = [m.id for m in SERIES.meetings]


def _named(name: str) -> type[Exception]:
    """An exception class with the name the provider's SDK gives its own."""
    return type(name, (Exception,), {})


class TestErrorKind:
    @pytest.mark.parametrize("name", [
        "RateLimitError", "InternalServerError", "APITimeoutError", "APIConnectionError",
        "TimeoutError",
    ])
    def test_a_rate_limit_server_error_or_timeout_is_the_providers(self, name: str) -> None:
        assert error_kind(name) is ErrorKind.PROVIDER

    @pytest.mark.parametrize("name", ["BadRequestError", "UnprocessableEntityError"])
    def test_a_request_the_provider_refused_is_the_systems(self, name: str) -> None:
        """The runtime built the request, so a refusal is the system under test failing."""
        assert error_kind(name) is ErrorKind.SYSTEM

    @pytest.mark.parametrize("name", [
        "AuthenticationError", "PermissionDeniedError", "NotFoundError", "KeyError",
        "SomethingNewError",
    ])
    def test_anything_else_is_a_harness_fault_never_a_guess(self, name: str) -> None:
        """A wrong key or model is the harness's setup; an error it cannot
        place is not guessed at."""
        assert error_kind(name) is ErrorKind.HARNESS

    def test_every_name_is_a_class_the_sdk_or_python_raises(self) -> None:
        """A misspelled name would never match, and its error would stop the run."""
        for name in PROVIDER_ERRORS | SYSTEM_ERRORS:
            cls = getattr(anthropic, name, None) or getattr(builtins, name, None)
            assert isinstance(cls, type) and issubclass(cls, Exception), name


class _Script:
    """A hold that answers each (meeting, attempt, try) as scripted: by
    default a result with no failed calls."""

    def __init__(self, **script: Held[str]) -> None:
        self.script = script
        self.held: list[tuple[str, int, int]] = []

    async def __call__(self, meeting: Meeting, attempt: int, meeting_try: int) -> Held[str]:
        self.held.append((meeting.id, attempt, meeting_try))
        key = f"{meeting.id}_{attempt}_{meeting_try}".replace("-", "_")
        return self.script.get(key, Held(f"{meeting.id}/{attempt}/{meeting_try}"))


_RATE_LIMITED: Held[str] = Held(None, errors=("RateLimitError",))
_REFUSED: Held[str] = Held(None, errors=("BadRequestError",))
_NOT_STARTED: Held[str] = Held(None, start_failed=True)
_PLAN = IDS[1].replace("-", "_")


class TestRunSeries:
    def test_the_numbers_are_the_pre_registered_ones(self) -> None:
        """Retried up to three times; fewer than four series left is incomplete."""
        assert (RETRIES, MIN_SERIES) == (3, 4)

    async def test_each_meeting_is_held_once_when_nothing_fails(self) -> None:
        hold = _Script()
        run = await run_series("C", SERIES, hold)
        assert hold.held == [(m, 1, 1) for m in IDS]
        assert (run.finished_attempt, run.dropped) == (1, False)
        assert run.finished_tries() == dict.fromkeys(IDS, 1)
        assert [t.held.result for t in run.finished().values()] == [f"{m}/1/1" for m in IDS]

    async def test_a_provider_error_holds_the_meeting_again_as_its_next_try(self) -> None:
        hold = _Script(**{f"{_PLAN}_1_1": _RATE_LIMITED})
        run = await run_series("C", SERIES, hold)
        assert hold.held[1:3] == [(IDS[1], 1, 1), (IDS[1], 1, 2)]
        assert run.finished_tries()[IDS[1]] == 2
        # The cut-short try is kept, so its spend is reported.
        assert [(t.meeting, t.meeting_try) for t in run.tries][1:3] == [(IDS[1], 1), (IDS[1], 2)]

    async def test_three_retries_then_the_series_starts_again_from_its_briefing(self) -> None:
        failing = {f"{_PLAN}_1_{n}": _RATE_LIMITED for n in range(1, RETRIES + 2)}
        hold = _Script(**failing)
        run = await run_series("C", SERIES, hold)
        assert hold.held[:RETRIES + 2] == [
            (IDS[0], 1, 1), *((IDS[1], 1, n) for n in range(1, RETRIES + 2)),
        ]
        assert hold.held[RETRIES + 2:] == [(m, 2, 1) for m in IDS]
        assert run.finished_attempt == 2
        assert all(t.attempt == 2 for t in run.finished().values())

    async def test_a_series_that_fails_again_is_dropped(self) -> None:
        failing = {
            f"{_PLAN}_{a}_{n}": _RATE_LIMITED for a in (1, 2) for n in range(1, RETRIES + 2)
        }
        run = await run_series("C", SERIES, _Script(**failing))
        assert (run.finished_attempt, run.dropped) == (None, True)
        assert run.finished() == {}
        assert len(run.tries) == 2 * (1 + RETRIES + 1)

    async def test_a_failure_the_system_causes_is_not_retried(self) -> None:
        hold = _Script(**{f"{_PLAN}_1_1": _REFUSED})
        run = await run_series("C", SERIES, hold)
        assert hold.held == [(m, 1, 1) for m in IDS]
        assert run.finished()[IDS[1]].held == _REFUSED

    async def test_a_deployment_that_does_not_start_is_held_again(self) -> None:
        hold = _Script(**{f"{_PLAN}_1_1": _NOT_STARTED})
        run = await run_series("C", SERIES, hold)
        assert run.finished_tries()[IDS[1]] == 2

    async def test_one_that_never_starts_is_a_harness_fault(self) -> None:
        failing = {f"{_PLAN}_1_{n}": _NOT_STARTED for n in range(1, RETRIES + 2)}
        with pytest.raises(HarnessFault, match=f"{IDS[1]}.*did not start"):
            await run_series("C", SERIES, _Script(**failing))

    async def test_a_failed_call_the_harness_cannot_place_stops_the_run(self) -> None:
        hold = _Script(**{f"{_PLAN}_1_1": Held("memo", errors=("AuthenticationError",))})
        with pytest.raises(HarnessFault, match="AuthenticationError"):
            await run_series("C", SERIES, hold)


def _run(series: str, finished_attempt: int | None) -> SeriesRun[str]:
    return SeriesRun("C", series, (), finished_attempt)


class TestSeriesKept:
    def test_a_series_dropped_in_one_arm_is_dropped_from_every_arms_comparisons(self) -> None:
        scored = [f"series-{n}" for n in range(1, 6)]
        runs = [_run(s, 1) for s in scored] + [
            SeriesRun("D", "series-3", (), None), SeriesRun("A", "series-3", (), 1),
        ]
        kept = series_kept(runs, scored)
        assert (kept.kept, kept.dropped, kept.complete) == (
            ("series-1", "series-2", "series-4", "series-5"), ("series-3",), True,
        )

    def test_fewer_than_four_series_left_leaves_the_run_incomplete(self) -> None:
        scored = [f"series-{n}" for n in range(1, 6)]
        runs = [_run(s, None if s in {"series-1", "series-2"} else 1) for s in scored]
        kept = series_kept(runs, scored)
        assert len(kept.kept) == MIN_SERIES - 1
        assert kept.complete is False


class _Provider:
    """Answers each call in turn: a text as a finished reply, an exception raised."""

    name = "anthropic"
    supports_prompt_cache = True

    def __init__(self, *outcomes: Exception | str) -> None:
        self.outcomes = list(outcomes)

    async def create_message(self, **_: Any) -> LLMResponse:
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return LLMResponse(text=outcome, usage=Usage(1200, 300))

    def format_tool_definitions(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return tools

    def append_tool_round(
        self, messages: list[Any], response: LLMResponse, tool_results: list[LLMToolResult],
    ) -> list[Any]:
        raise AssertionError("arm A sends no tools")


class TestArmA:
    async def test_a_provider_error_is_retried_and_each_try_is_logged(
        self, tmp_path: Path,
    ) -> None:
        log_path = tmp_path / "calls.jsonl"
        client = LLMClient(_Provider("noted", _named("RateLimitError")("429"), "memo", "", "", ""))
        run = await run_series("A", SERIES, arm_a_hold(client, PANEL, SERIES, log_path=log_path))
        reply = run.finished()[IDS[1]].held.result
        assert isinstance(reply, ArmAReply) and reply.text == "memo"
        log = read_call_log(log_path)
        assert [(f.meeting, f.meeting_try, f.error) for f in log.failures] == [
            (IDS[1], 1, "RateLimitError"),
        ]
        assert [(r.meeting, r.meeting_try) for r in log.records][:2] == [
            (IDS[0], 1), (IDS[1], 2),
        ]

    async def test_a_refused_request_leaves_the_meeting_without_a_reply(
        self, tmp_path: Path,
    ) -> None:
        client = LLMClient(_Provider("noted", _named("BadRequestError")("400"), "", "", ""))
        run = await run_series(
            "A", SERIES, arm_a_hold(client, PANEL, SERIES, log_path=tmp_path / "c.jsonl"),
        )
        assert run.finished()[IDS[1]].held == Held(None, errors=("BadRequestError",))

    async def test_an_error_the_harness_cannot_place_is_a_harness_fault(
        self, tmp_path: Path,
    ) -> None:
        client = LLMClient(_Provider(_named("AuthenticationError")("401")))
        with pytest.raises(HarnessFault, match="AuthenticationError") as caught:
            await run_series(
                "A", SERIES, arm_a_hold(client, PANEL, SERIES, log_path=tmp_path / "c.jsonl"),
            )
        assert type(caught.value.__cause__).__name__ == "AuthenticationError"


class _Meetings:
    """Stands in for ``channel_arm.run_meeting``: each call acts as scripted,
    by try, and may write a line to the meeting's call log first."""

    def __init__(self, *acts: Exception | Sequence[str]) -> None:
        self.acts = list(acts)
        self.directories: list[Path] = []

    async def __call__(
        self, panel: Any, arm: str, series: Any, meeting: Meeting, *, attempt: int,
        meeting_try: int, directory: Path, **options: Any,
    ) -> Any:  # a ChannelMeeting in the harness; its name is enough here
        self.directories.append(directory)
        directory.mkdir(parents=True)
        act = self.acts.pop(0) if self.acts else ()
        if isinstance(act, Exception):
            raise act
        tags = {"arm": arm, "series": series.id, "meeting": meeting.id,
                "meeting_kind": meeting.kind.value, "attempt": str(attempt),
                "try": str(meeting_try)}
        with (directory / CALL_LOG).open("a") as log:
            for error in act:
                log.write(json.dumps({
                    "tags": tags, "agent_id": "ripple-kite", "purpose": "turn",
                    "provider": "anthropic", "model": "claude-sonnet-4-6", "model_alias": None,
                    "started_at": "2026-10-01T09:00:00+00:00", "input_tokens": 0,
                    "output_tokens": 0, "cache_write_tokens": 0, "cache_read_tokens": 0,
                    "error": error,
                }) + "\n")
        return f"{meeting.id}/{attempt}/{meeting_try}"


class TestChannelArms:
    async def test_each_try_has_a_directory_of_its_own(self, tmp_path: Path) -> None:
        meetings = _Meetings((), ("InternalServerError",))
        hold = channel_hold(PANEL, "B", SERIES, tmp_path, run=meetings)
        run = await run_series("B", SERIES, hold)
        assert meetings.directories[:3] == [
            try_directory(tmp_path, 1, SERIES.meetings[0], 1),
            try_directory(tmp_path, 1, SERIES.meetings[1], 1),
            try_directory(tmp_path, 1, SERIES.meetings[1], 2),
        ]
        assert meetings.directories[2] == tmp_path / "attempt-1" / IDS[1] / "try-2"
        assert run.finished()[IDS[1]].held == Held(f"{IDS[1]}/1/2")

    async def test_a_provider_error_in_the_call_log_holds_the_meeting_again(
        self, tmp_path: Path,
    ) -> None:
        meetings = _Meetings((), ("RateLimitError", "BadRequestError"))
        hold = channel_hold(PANEL, "C", SERIES, tmp_path, run=meetings)
        run = await run_series("C", SERIES, hold)
        cut_short = run.tries[1]
        assert (cut_short.meeting_try, cut_short.held.errors) == (
            1, ("RateLimitError", "BadRequestError"),
        )
        assert run.finished_tries()[IDS[1]] == 2

    async def test_a_deployment_that_does_not_start_is_held_again(self, tmp_path: Path) -> None:
        meetings = _Meetings((), StartError("ripple-kite exited (1) while starting"))
        hold = channel_hold(PANEL, "B", SERIES, tmp_path, run=meetings)
        run = await run_series("B", SERIES, hold)
        assert run.tries[1].held == Held(None, start_failed=True)
        assert run.finished_tries()[IDS[1]] == 2

    @pytest.mark.parametrize("error", [
        DeploymentError("the orchestrator's rate limiter is on"),
        DeploymentError("a spending limit the deployment turns off refused leases (check 6)"),
        OrchestratorError("a message the harness cannot read"),
    ])
    async def test_anything_else_the_meeting_raises_is_a_harness_fault(
        self, tmp_path: Path, error: Exception,
    ) -> None:
        meetings = _Meetings((), error)
        with pytest.raises(HarnessFault, match=f"B, {SERIES.id}, {IDS[1]}, attempt 1, try 1") as c:
            await run_series("B", SERIES, channel_hold(PANEL, "B", SERIES, tmp_path, run=meetings))
        assert c.value.__cause__ is error

    async def test_a_call_log_the_harness_cannot_read_is_a_harness_fault(
        self, tmp_path: Path,
    ) -> None:
        class _Broken(_Meetings):
            async def __call__(self, *args: Any, directory: Path, **kwargs: Any) -> Any:
                result = await super().__call__(*args, directory=directory, **kwargs)
                (directory / CALL_LOG).write_text("{broken\n")
                return result

        with pytest.raises(HarnessFault, match="calls.jsonl:1"):
            await run_series("C", SERIES, channel_hold(PANEL, "C", SERIES, tmp_path, run=_Broken()))
