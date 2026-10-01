"""EXP-001 harness — attempts and failures: holding arm A's and arms B/C's meetings (PR 5b).

Arm A's hold makes its one call through a real ``LLMClient`` and call log;
the channel arms' hold runs a stand-in for ``deployed_meeting.run_meeting``
that writes call-log lines. The rules both holds serve are in
``test_exp001_attempts.py``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from agents.llm_client import LLMClient, LLMResponse, Usage
from agents.llm_types import LLMToolResult
from evaluators.exp001.arm_a import ArmAReply
from evaluators.exp001.attempts import (
    RETRIES,
    HarnessFault,
    Held,
    arm_a_hold,
    channel_hold,
    run_series,
    try_directory,
)
from evaluators.exp001.deployed_meeting import CALL_LOG, RECORD
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


async def _no_wait(seconds: float) -> None:
    """Stands in for asyncio.sleep between tries."""


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
        run = await run_series(
            "A", SERIES, arm_a_hold(client, PANEL, SERIES, log_path=log_path), sleep=_no_wait,
        )
        reply = run.finished()[IDS[1]].held.result
        assert isinstance(reply, ArmAReply) and reply.text == "memo"
        log = read_call_log(log_path)
        assert [(f.meeting, f.meeting_try, f.error) for f in log.failures] == [
            (IDS[1], 1, "RateLimitError"),
        ]
        assert [(r.meeting, r.meeting_try) for r in log.records][:2] == [
            (IDS[0], 1), (IDS[1], 2),
        ]

    async def test_a_restart_tags_its_calls_with_the_second_attempt(
        self, tmp_path: Path,
    ) -> None:
        log_path = tmp_path / "calls.jsonl"
        limited = [_named("RateLimitError")("429") for _ in range(RETRIES + 1)]
        client = LLMClient(_Provider("noted", *limited, "noted", "memo", "", ""))
        run = await run_series(
            "A", SERIES, arm_a_hold(client, PANEL, SERIES, log_path=log_path), sleep=_no_wait,
        )
        assert run.finished_attempt == 2
        records = read_call_log(log_path).records
        assert [(r.meeting, r.attempt, r.meeting_try) for r in records][1:3] == [
            (IDS[0], 2, 1), (IDS[1], 2, 1),
        ]

    async def test_a_refused_request_is_a_harness_fault(self, tmp_path: Path) -> None:
        """The harness builds arm A's one request, and a spend limit the
        provider enforces is refused with the same 400."""
        client = LLMClient(_Provider("noted", _named("BadRequestError")("400")))
        with pytest.raises(HarnessFault, match=(
            f"^A, {SERIES.id}, {IDS[1]}, attempt 1, try 1: BadRequestError: 400"
        )):
            await run_series(
                "A", SERIES, arm_a_hold(client, PANEL, SERIES, log_path=tmp_path / "c.jsonl"),
            )

    async def test_an_error_the_harness_cannot_place_is_a_harness_fault(
        self, tmp_path: Path,
    ) -> None:
        client = LLMClient(_Provider(_named("AuthenticationError")("401")))
        with pytest.raises(HarnessFault, match=(
            f"^A, {SERIES.id}, {IDS[0]}, attempt 1, try 1: AuthenticationError: 401"
        )) as caught:
            await run_series(
                "A", SERIES, arm_a_hold(client, PANEL, SERIES, log_path=tmp_path / "c.jsonl"),
            )
        assert type(caught.value.__cause__).__name__ == "AuthenticationError"

    async def test_the_replies_held_before_a_fault_ride_on_it(self, tmp_path: Path) -> None:
        """Arm A's replies are kept nowhere else."""
        client = LLMClient(_Provider("noted", "memo", _named("AuthenticationError")("401")))
        with pytest.raises(HarnessFault) as caught:
            await run_series(
                "A", SERIES, arm_a_hold(client, PANEL, SERIES, log_path=tmp_path / "c.jsonl"),
            )
        replies = [t.held.result for t in caught.value.tries]
        assert [r.text for r in replies if isinstance(r, ArmAReply)] == ["noted", "memo"]

    def test_a_log_that_exists_is_refused(self, tmp_path: Path) -> None:
        """An earlier run's calls would be counted again."""
        log_path = tmp_path / "calls.jsonl"
        log_path.write_text("")
        client = LLMClient(_Provider())
        with pytest.raises(FileExistsError, match="calls.jsonl"):
            arm_a_hold(client, PANEL, SERIES, log_path=log_path)

    async def test_the_logs_folder_is_made(self, tmp_path: Path) -> None:
        """The runtime drops a line it cannot write; arm A's spend would vanish."""
        log_path = tmp_path / "A" / "practice" / "calls.jsonl"
        client = LLMClient(_Provider("noted", "memo", "", ""))
        await run_series("A", SERIES, arm_a_hold(client, PANEL, SERIES, log_path=log_path))
        assert len(read_call_log(log_path).records) == len(IDS)

    async def test_each_call_names_the_model_the_hold_is_given(self, tmp_path: Path) -> None:
        """Offline arm A's call goes to the mock, and names its model, as every alias does."""
        log_path = tmp_path / "calls.jsonl"
        client = LLMClient(_Provider("noted", "memo", "", ""))
        await run_series(
            "A", SERIES, arm_a_hold(client, PANEL, SERIES, log_path=log_path, model="offline"),
        )
        assert {r.model for r in read_call_log(log_path).records} == {"offline"}


_Line = str | tuple[str, str] | None  # an error, (purpose, error), or a call that answered


class _Meetings:
    """Stands in for ``deployed_meeting.run_meeting``: each call acts as scripted,
    by try, and may write lines to the meeting's call log first: a failed
    turn for each error named, (purpose, error) for another purpose, and a
    call that answered for None."""

    def __init__(self, *acts: Exception | Sequence[_Line]) -> None:
        self.acts = list(acts)
        self.directories: list[Path] = []
        self.attempts: list[int] = []

    async def __call__(
        self, panel: Any, arm: str, series: Any, meeting: Meeting, *, attempt: int,
        meeting_try: int, directory: Path, ended: Any,
    ) -> Any:  # a ChannelMeeting in the harness; its name is enough here
        self.directories.append(directory)
        self.attempts.append(attempt)
        directory.mkdir(parents=True)
        act = self.acts.pop(0) if self.acts else ()
        if isinstance(act, Exception):
            raise act
        tags = {"arm": arm, "series": series.id, "meeting": meeting.id,
                "meeting_kind": meeting.kind.value, "attempt": str(attempt),
                "try": str(meeting_try)}
        with (directory / CALL_LOG).open("a") as log:
            for line in act:
                purpose, error = line if isinstance(line, tuple) else ("turn", line)
                log.write(json.dumps({
                    "tags": tags, "agent_id": "ripple-kite", "purpose": purpose,
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
        run = await run_series("B", SERIES, hold, sleep=_no_wait)
        assert meetings.directories[:3] == [
            try_directory(tmp_path, 1, SERIES.meetings[0], 1),
            try_directory(tmp_path, 1, SERIES.meetings[1], 1),
            try_directory(tmp_path, 1, SERIES.meetings[1], 2),
        ]
        assert meetings.directories[2] == tmp_path / "attempt-1" / IDS[1] / "try-2"
        assert run.finished()[IDS[1]].held == Held(f"{IDS[1]}/1/2")

    async def test_a_restart_holds_each_meeting_in_a_directory_of_the_second_attempt(
        self, tmp_path: Path,
    ) -> None:
        meetings = _Meetings((), *[("RateLimitError",)] * (RETRIES + 1))
        hold = channel_hold(PANEL, "B", SERIES, tmp_path, run=meetings)
        run = await run_series("B", SERIES, hold, sleep=_no_wait)
        restart = 1 + RETRIES + 1  # the briefing, then every try of the plan meeting
        assert meetings.directories[restart] == tmp_path / "attempt-2" / IDS[0] / "try-1"
        assert meetings.attempts[restart:] == [2] * len(IDS)
        assert run.finished_attempt == 2

    async def test_a_relative_root_is_made_absolute(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The orchestrator runs in its deployment's directory, so a path
        relative to the harness's would point elsewhere."""
        monkeypatch.chdir(tmp_path)
        meetings = _Meetings()
        await run_series("B", SERIES, channel_hold(PANEL, "B", SERIES, Path("runs"), run=meetings))
        assert meetings.directories[0] == (tmp_path / "runs").resolve() / "attempt-1" / IDS[0] / (
            "try-1"
        )

    async def test_a_provider_error_in_the_call_log_holds_the_meeting_again(
        self, tmp_path: Path,
    ) -> None:
        meetings = _Meetings((), ("RateLimitError", "BadRequestError"))
        hold = channel_hold(PANEL, "C", SERIES, tmp_path, run=meetings)
        run = await run_series("C", SERIES, hold, sleep=_no_wait)
        cut_short = run.tries[1]
        assert (cut_short.meeting_try, cut_short.held.errors) == (
            1, ("RateLimitError", "BadRequestError"),
        )
        assert run.finished_tries()[IDS[1]] == 2

    async def test_a_failed_memory_write_outside_the_arms_design_does_not_hold_it_again(
        self, tmp_path: Path,
    ) -> None:
        """B and C have no memory: a summary that failed changes nothing the
        meeting shows. A turn cut off at the runtime's limit still does."""
        meetings = _Meetings((), (("summary", "RateLimitError"), ("summary", "CancelledError")))
        hold = channel_hold(PANEL, "B", SERIES, tmp_path, run=meetings)
        run = await run_series("B", SERIES, hold)
        assert run.finished_tries()[IDS[1]] == 1
        assert run.finished()[IDS[1]].held.errors == ()

    async def test_every_call_refused_is_a_harness_fault(self, tmp_path: Path) -> None:
        """A spend limit or an empty balance is refused with the same 400 as
        a malformed request, but it refuses every call."""
        meetings = _Meetings((), ("BadRequestError", "BadRequestError"))
        hold = channel_hold(PANEL, "C", SERIES, tmp_path, run=meetings)
        with pytest.raises(HarnessFault, match=(
            f"^C, {SERIES.id}, {IDS[1]}, attempt 1, try 1: every call was refused"
        )):
            await run_series("C", SERIES, hold)

    async def test_one_refused_call_among_answered_ones_stands(self, tmp_path: Path) -> None:
        meetings = _Meetings((), (None, "BadRequestError"))
        hold = channel_hold(PANEL, "C", SERIES, tmp_path, run=meetings)
        run = await run_series("C", SERIES, hold)
        assert run.finished()[IDS[1]].held == Held(f"{IDS[1]}/1/1", errors=("BadRequestError",))

    async def test_a_deployment_that_does_not_start_is_held_again(self, tmp_path: Path) -> None:
        meetings = _Meetings((), StartError("ripple-kite exited (1) while starting"))
        hold = channel_hold(PANEL, "B", SERIES, tmp_path, run=meetings)
        run = await run_series("B", SERIES, hold, sleep=_no_wait)
        assert run.tries[1].held == Held(None, start_failed=True)
        assert run.finished_tries()[IDS[1]] == 2

    async def test_the_last_start_failure_says_why_the_run_stopped(self, tmp_path: Path) -> None:
        errors = [StartError(f"orchestrator exited ({n}) while starting") for n in range(4)]
        meetings = _Meetings((), *errors)
        hold = channel_hold(PANEL, "B", SERIES, tmp_path, run=meetings)
        with pytest.raises(HarnessFault, match=r"did not start in 4 tries.*exited \(3\)") as c:
            await run_series("B", SERIES, hold, sleep=_no_wait)
        assert c.value.__cause__ is errors[-1]

    async def test_an_orchestrator_that_exits_mid_meeting_is_recorded_not_a_fault(
        self, tmp_path: Path,
    ) -> None:
        """The orchestrator is part of the system under test, as an adviser is."""

        class _Crash(_Meetings):
            async def __call__(self, *args: Any, directory: Path, **kwargs: Any) -> Any:
                if len(self.directories) != 1:
                    return await super().__call__(*args, directory=directory, **kwargs)
                await super().__call__(*args, directory=directory, **kwargs)
                (directory / RECORD).write_text(json.dumps({"exited": {"orchestrator": 2}}))
                raise OrchestratorError("GET …/messages: ClientConnectorError()")

        hold = channel_hold(PANEL, "C", SERIES, tmp_path, run=_Crash())
        run = await run_series("C", SERIES, hold)
        assert run.finished()[IDS[1]].held == Held(None)

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

    async def test_a_call_log_that_is_not_text_is_a_harness_fault(self, tmp_path: Path) -> None:
        class _Garbled(_Meetings):
            async def __call__(self, *args: Any, directory: Path, **kwargs: Any) -> Any:
                result = await super().__call__(*args, directory=directory, **kwargs)
                (directory / CALL_LOG).write_bytes(b"\xff\xfe{}\n")
                return result

        hold = channel_hold(PANEL, "C", SERIES, tmp_path, run=_Garbled())
        with pytest.raises(HarnessFault, match="UnicodeDecodeError"):
            await run_series("C", SERIES, hold)

    async def test_a_try_ends_at_its_first_provider_error(self, tmp_path: Path) -> None:
        """The meeting is held again whatever follows, so it is not waited out."""
        stopped: list[bool] = []

        class _Stalls(_Meetings):
            async def __call__(self, *args: Any, directory: Path, **kwargs: Any) -> Any:
                result = await super().__call__(*args, directory=directory, **kwargs)
                if len(self.directories) != 2:
                    return result
                try:
                    await asyncio.sleep(3)  # a discussion that runs on, then closes
                finally:
                    stopped.append(True)
                return result

        meetings = _Stalls((), ("RateLimitError",))
        hold = channel_hold(PANEL, "C", SERIES, tmp_path, run=meetings, watch_seconds=0.01)
        run = await run_series("C", SERIES, hold, sleep=_no_wait)
        assert stopped == [True]
        # Ended before the discussion closed, so it has no result.
        assert run.tries[1].held == Held(None, errors=("RateLimitError",))
        assert run.finished_tries()[IDS[1]] == 2

    async def test_a_cancel_from_outside_still_stops_the_series(self, tmp_path: Path) -> None:
        """A hangup cancels the meeting as Ctrl-C does; it is no try cut short."""

        class _Stalls(_Meetings):
            async def __call__(self, *args: Any, directory: Path, **kwargs: Any) -> Any:
                result = await super().__call__(*args, directory=directory, **kwargs)
                if len(self.directories) == 1:
                    await asyncio.sleep(60)
                return result

        hold = channel_hold(PANEL, "C", SERIES, tmp_path, run=_Stalls(), watch_seconds=60)
        series = asyncio.ensure_future(run_series("C", SERIES, hold))
        await asyncio.sleep(0.05)
        series.cancel()
        with pytest.raises(asyncio.CancelledError):
            await series
