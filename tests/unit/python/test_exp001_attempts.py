"""EXP-001 harness — attempts and failures, for every arm (PR 5b).

Pre-registration §3 sorts what can go wrong at a meeting into three kinds.
A provider error is retried up to three times, by holding the meeting again;
a series whose meeting still fails starts again from its briefing, once, and
is then dropped from every arm's comparisons. A failure the system causes is
recorded and the meeting stands. A harness fault stops the run. How arm A
and the channel arms hold one try is in ``test_exp001_attempts_holds.py``.
"""

from __future__ import annotations

import asyncio
import builtins
from pathlib import Path

import anthropic
import anthropic._exceptions
import httpx
import pytest

from evaluators.exp001.attempts import (
    MIN_SERIES,
    PROVIDER_ERRORS,
    RESTART_WAIT,
    RETRIES,
    RETRY_WAITS,
    SYSTEM_ERRORS,
    ErrorKind,
    HarnessFault,
    Held,
    RunStopped,
    SeriesRun,
    Try,
    error_kind,
    run_series,
    series_kept,
)
from evaluators.exp001.materials import Meeting, load_series

_EXP = Path(__file__).resolve().parents[3] / "evaluators" / "experiments" / "EXP-001"
SERIES = load_series(_EXP / "practice.yaml")
IDS = [m.id for m in SERIES.meetings]


class TestErrorKind:
    @pytest.mark.parametrize("name", [
        "RateLimitError", "InternalServerError", "OverloadedError", "ConflictError",
        "APITimeoutError", "APIConnectionError", "CancelledError",
    ])
    def test_a_rate_limit_server_error_or_timeout_is_the_providers(self, name: str) -> None:
        """CancelledError is how the call log records a call the runtime cut
        off at its own time limit: a 300-second turn, a 30-second summary."""
        assert error_kind(name) is ErrorKind.PROVIDER

    @pytest.mark.parametrize("name", ["BadRequestError", "UnprocessableEntityError"])
    def test_a_request_the_provider_refused_is_the_systems(self, name: str) -> None:
        """The runtime built the request, so a refusal is the system under test failing."""
        assert error_kind(name) is ErrorKind.SYSTEM

    @pytest.mark.parametrize("name", [
        "AuthenticationError", "PermissionDeniedError", "NotFoundError", "KeyError",
        "SomethingNewError", "TimeoutError", "APIStatusError",
    ])
    def test_anything_else_is_a_harness_fault_never_a_guess(self, name: str) -> None:
        """A wrong key or model is the harness's setup; an error it cannot
        place is not guessed at. The SDK reports its own timeouts as
        APITimeoutError, so a bare TimeoutError came from the harness's side;
        a bare APIStatusError may be a 402 billing error."""
        assert error_kind(name) is ErrorKind.HARNESS

    def test_every_name_is_a_class_the_sdk_or_python_raises(self) -> None:
        """A misspelled name would never match, and its error would stop the run.
        Not every SDK exports every class at its top level."""
        for name in PROVIDER_ERRORS | SYSTEM_ERRORS:
            cls = next(
                (c for c in (getattr(m, name, None) for m in (
                    anthropic, anthropic._exceptions, builtins, asyncio)) if c is not None),
                None,
            )
            assert isinstance(cls, type) and issubclass(cls, BaseException), name

    @pytest.mark.parametrize(("status", "kind"), [
        (400, ErrorKind.SYSTEM), (422, ErrorKind.SYSTEM),
        (409, ErrorKind.PROVIDER), (429, ErrorKind.PROVIDER), (500, ErrorKind.PROVIDER),
        (502, ErrorKind.PROVIDER), (503, ErrorKind.PROVIDER), (504, ErrorKind.PROVIDER),
        (529, ErrorKind.PROVIDER),
        (401, ErrorKind.HARNESS), (402, ErrorKind.HARNESS), (403, ErrorKind.HARNESS),
        (404, ErrorKind.HARNESS), (408, ErrorKind.HARNESS), (413, ErrorKind.HARNESS),
    ])
    def test_each_status_is_placed_by_the_class_the_sdk_raises_for_it(
        self, status: int, kind: ErrorKind,
    ) -> None:
        """The other direction: what the installed SDK actually raises. 408
        stays a harness fault: it arrives as a bare APIStatusError, as a 402
        billing error does, and the Claude API does not send it."""
        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        error = anthropic.Anthropic(api_key="x")._make_status_error(
            "error", body=None, response=httpx.Response(status, request=request),
        )
        assert error_kind(type(error).__name__) is kind, type(error).__name__


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


async def _no_wait(seconds: float) -> None:
    """Stands in for asyncio.sleep between tries."""


class _Clock:
    """Stands in for asyncio.sleep, and keeps every wait asked for."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


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
        run = await run_series("C", SERIES, hold, sleep=_no_wait)
        assert hold.held[1:3] == [(IDS[1], 1, 1), (IDS[1], 1, 2)]
        assert run.finished_tries()[IDS[1]] == 2
        # The cut-short try is kept, so its spend is reported.
        assert [(t.meeting, t.meeting_try) for t in run.tries][1:3] == [(IDS[1], 1), (IDS[1], 2)]

    async def test_three_retries_then_the_series_starts_again_from_its_briefing(self) -> None:
        failing = {f"{_PLAN}_1_{n}": _RATE_LIMITED for n in range(1, RETRIES + 2)}
        hold = _Script(**failing)
        run = await run_series("C", SERIES, hold, sleep=_no_wait)
        assert hold.held[:RETRIES + 2] == [
            (IDS[0], 1, 1), *((IDS[1], 1, n) for n in range(1, RETRIES + 2)),
        ]
        assert hold.held[RETRIES + 2:] == [(m, 2, 1) for m in IDS]
        assert run.finished_attempt == 2
        assert all(t.attempt == 2 for t in run.finished().values())

    async def test_it_waits_longer_before_each_try_and_before_starting_again(self) -> None:
        """So a provider outage of a few minutes does not use up the retries."""
        failing = {f"{_PLAN}_1_{n}": _RATE_LIMITED for n in range(1, RETRIES + 2)}
        clock = _Clock()
        await run_series("C", SERIES, _Script(**failing), sleep=clock)
        assert clock.waits == [*RETRY_WAITS, RESTART_WAIT]
        assert (RETRY_WAITS, RESTART_WAIT) == ((60.0, 300.0, 900.0), 1800.0)

    async def test_a_series_that_fails_again_is_dropped(self) -> None:
        failing = {
            f"{_PLAN}_{a}_{n}": _RATE_LIMITED for a in (1, 2) for n in range(1, RETRIES + 2)
        }
        run = await run_series("C", SERIES, _Script(**failing), sleep=_no_wait)
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
        run = await run_series("C", SERIES, hold, sleep=_no_wait)
        assert run.finished_tries()[IDS[1]] == 2

    async def test_a_start_failure_uses_none_of_the_retries(self) -> None:
        """Nothing of the meeting happened, so only the provider errors count:
        two here, inside the three retries."""
        tries = [_NOT_STARTED, _RATE_LIMITED, _NOT_STARTED, _RATE_LIMITED]
        hold = _Script(**{f"{_PLAN}_1_{n}": h for n, h in enumerate(tries, start=1)})
        run = await run_series("B", SERIES, hold, sleep=_no_wait)
        assert (run.finished_attempt, run.finished_tries()[IDS[1]]) == (1, 5)

    async def test_one_that_never_starts_is_a_harness_fault(self) -> None:
        failing = {f"{_PLAN}_1_{n}": _NOT_STARTED for n in range(1, RETRIES + 2)}
        with pytest.raises(HarnessFault, match=f"{IDS[1]}.*did not start"):
            await run_series("C", SERIES, _Script(**failing), sleep=_no_wait)

    async def test_four_start_failures_are_a_harness_fault_whatever_came_between(self) -> None:
        tries = [_RATE_LIMITED, *[_NOT_STARTED] * (RETRIES + 1)]
        hold = _Script(**{f"{_PLAN}_1_{n}": h for n, h in enumerate(tries, start=1)})
        with pytest.raises(HarnessFault, match=f"{IDS[1]}, attempt 1, try 5: .*did not start"):
            await run_series("B", SERIES, hold, sleep=_no_wait)

    async def test_a_failed_call_the_harness_cannot_place_stops_the_run(self) -> None:
        hold = _Script(**{f"{_PLAN}_1_1": Held("memo", errors=("AuthenticationError",))})
        with pytest.raises(HarnessFault, match="AuthenticationError"):
            await run_series("C", SERIES, hold)

    async def test_the_tries_held_before_a_fault_ride_on_it(self) -> None:
        """They are outputs the pre-registration discards but publishes."""
        hold = _Script(**{f"{_PLAN}_1_1": Held(None, errors=("AuthenticationError",))})
        with pytest.raises(HarnessFault) as caught:
            await run_series("C", SERIES, hold)
        assert caught.value.tries == (Try(IDS[0], 1, 1, Held(f"{IDS[0]}/1/1")),)

    async def test_anything_a_hold_raises_is_a_harness_fault_naming_where(self) -> None:
        error = PermissionError("the store could not be copied")

        async def hold(meeting: Meeting, attempt: int, meeting_try: int) -> Held[str]:
            if meeting.id == IDS[1]:
                raise error
            return Held(meeting.id)

        with pytest.raises(HarnessFault, match=(
            f"^C, {SERIES.id}, {IDS[1]}, attempt 1, try 1: PermissionError: the store"
        )) as caught:
            await run_series("C", SERIES, hold)
        assert caught.value.__cause__ is error

    async def test_a_rule_of_the_run_stops_it_with_no_fault(self) -> None:
        """The scored run's spend cap, or its seven-day window, refuses a try
        (PR 6c). That is no harness fault, and the tries before it ride on it
        as they ride on one."""
        stop = RunStopped("the $150 cap was reached")

        async def hold(meeting: Meeting, attempt: int, meeting_try: int) -> Held[str]:
            if meeting.id == IDS[1]:
                raise stop
            return Held(meeting.id)

        with pytest.raises(RunStopped) as caught:
            await run_series("C", SERIES, hold)
        assert caught.value is stop and not isinstance(stop, HarnessFault)
        assert stop.tries == (Try(IDS[0], 1, 1, Held(IDS[0])),)


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
