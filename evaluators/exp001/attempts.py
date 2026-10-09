"""Attempts and failures: what the harness does when a meeting goes wrong.

Pre-registration §3 sorts what can go wrong into three kinds, and this
module holds every arm's meetings by those rules.

- **A provider error**, such as a rate limit, a server error or a timeout, is
  retried up to three times: the meeting is held again, as its next try,
  after a wait that grows each time. A persona turn that ends in one
  publishes nothing, so it looks like silence, and the harness reads them
  from the call log instead, where a failed call's line names the
  exception's class; a call the runtime cut off at its own time limit is
  logged as cancelled. A channel-arm try ends at the first such line, but
  not at a failed keep-alive (arm D′), which changes nothing the meeting
  shows. If the meeting still fails after three retries, the arm's series
  starts again from its briefing as its second attempt. If that fails too, the
  series is dropped from every arm's comparisons. With fewer than four
  series left, the run is incomplete.
- **A failure the system causes** is not retried: the meeting stands as it
  went. A request the provider refused as malformed is one, since the
  runtime built it, unless every call of the try was refused: a spend limit
  or an empty balance is refused the same way. The channel arms record the
  others, such as a discussion that never closed, a missing memo or a
  process that exited, the orchestrator included.
- **A harness fault** stops the run: :class:`HarnessFault`. A wrong key or
  model, a deployment set up against the design (a rate limiter left on, a
  spending limit that refused a lease), a refused arm-A request (the harness
  builds it), an orchestrator or call log the harness cannot read, and any
  error it cannot place are harness faults; it never guesses.

A deployment that does not start is held again: the operator had not
spoken, so nothing of the meeting happened, and it uses none of the
retries. One that does not start four times is a harness fault.

A rule of the scored run can refuse a try too, once its $150 cap is reached
or its seven-day window has closed: :class:`RunStopped`, which is no fault.

Every try is kept, in the series' run or on the fault that stopped it, so a
cut-short try's spend is reported; dollars per plan count only the try that
finished (:meth:`SeriesRun.finished_tries`).
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import enum
import itertools
import json
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Generic, TypeVar

from agents.llm_client import LLMClient
from evaluators.exp001 import arm_a, channel_arm, deployed_meeting
from evaluators.exp001.costs import ARMS_MODEL, CallPurpose
from evaluators.exp001.deployment import StartError
from evaluators.exp001.materials import Meeting, Series
from evaluators.exp001.orchestrator import OrchestratorError
from evaluators.exp001.panel import Panel
from evaluators.exp001.runtime import FailedCall, read_call_log

T = TypeVar("T")

RETRIES = 3
ATTEMPTS = 2  # the first, and one start again from the briefing
MIN_SERIES = 4
HARNESS_FAULTS = 3  # the third ends the run, incomplete
# Seconds before a meeting's next try, and before a series starts again, so
# a provider outage of a few minutes does not use up the retries.
RETRY_WAITS = (60.0, 300.0, 900.0)
RESTART_WAIT = 1800.0

# Exception class names, as the call log records them. The provider's SDK
# retries all but the last twice itself before one reaches the log. A 408
# stays a harness fault: the SDK raises a bare APIStatusError for it, as for
# a 402 billing error, and the Claude API does not send it.
PROVIDER_ERRORS = frozenset({
    "RateLimitError",  # 429
    "InternalServerError",  # any 5xx but 529
    "OverloadedError",  # 529
    "ConflictError",  # 409, which the SDK retries as a lock timeout
    "APITimeoutError",
    "APIConnectionError",
    # A call the runtime cut off at its own limit: a turn at 300 seconds, a
    # memory summary at 30.
    "CancelledError",
})
# The provider refused the request the runtime built.
SYSTEM_ERRORS = frozenset({"BadRequestError", "UnprocessableEntityError"})


class ErrorKind(enum.Enum):
    PROVIDER = "provider"
    SYSTEM = "system"
    HARNESS = "harness"


def error_kind(name: str) -> ErrorKind:
    """Which of the three kinds a failed call's exception class is."""
    if name in PROVIDER_ERRORS:
        return ErrorKind.PROVIDER
    if name in SYSTEM_ERRORS:
        return ErrorKind.SYSTEM
    return ErrorKind.HARNESS


class HarnessFault(RuntimeError):  # noqa: N818 — pre-registration §3 vocabulary
    """The harness proved wrong: the run stops, and the fix goes through a
    reviewed PR (pre-registration §3). *tries* holds the stopped series'
    tries so far: outputs set aside, and published with the result."""

    tries: tuple[Try[Any], ...] = ()


class RunStopped(RuntimeError):  # noqa: N818 — pre-registration §3 vocabulary
    """A rule of the run refused a try: the scored run's spend cap was
    reached, or its seven-day window closed (pre-registration §3). It is no
    harness fault, so nothing is discarded. *tries* holds the stopped
    series' tries so far, as a fault's does."""

    tries: tuple[Try[Any], ...] = ()


@dataclass(frozen=True)
class Held(Generic[T]):
    """What one holding of a meeting gave.

    *result* is the arm's own record of the meeting, None when there is
    none: a deployment that did not start or whose orchestrator exited, a
    try ended at a provider error, or arm A's one call failing. *errors*
    names the exception class of every failed call that bears on it.
    """

    result: T | None
    errors: tuple[str, ...] = ()
    start_failed: bool = False
    # Why it did not start, for the fault the fourth time raises.
    start_error: BaseException | None = field(default=None, compare=False)

    @property
    def cut_short(self) -> bool:
        """Whether the meeting is held again: a provider error, or no start."""
        return self.start_failed or any(error_kind(e) is ErrorKind.PROVIDER for e in self.errors)


@dataclass(frozen=True)
class Try(Generic[T]):
    """One holding of a meeting: which attempt of the series, which try within it."""

    meeting: str
    attempt: int
    meeting_try: int
    held: Held[T]


# Holds *meeting* for the given attempt and try.
Hold = Callable[[Meeting, int, int], Awaitable[Held[T]]]


@dataclass(frozen=True)
class SeriesRun(Generic[T]):
    """One arm's series: every try, and the attempt that finished, if any did."""

    arm: str
    series: str
    tries: tuple[Try[T], ...]
    finished_attempt: int | None  # None: the series is dropped

    @property
    def dropped(self) -> bool:
        return self.finished_attempt is None

    def finished(self) -> dict[str, Try[T]]:
        """Each meeting's last try in the attempt that finished; empty if dropped."""
        return {t.meeting: t for t in self.tries if t.attempt == self.finished_attempt}

    def finished_tries(self) -> dict[str, int]:
        """The try that finished, for each meeting: what dollars per plan count."""
        return {meeting: t.meeting_try for meeting, t in self.finished().items()}


async def run_series(
    arm: str, series: Series, hold: Hold[T], *, retries: int = RETRIES,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> SeriesRun[T]:
    """Hold every meeting of *series* for *arm*, by the rules above."""
    tries: list[Try[T]] = []
    try:
        for attempt in range(1, ATTEMPTS + 1):
            if attempt > 1:
                await sleep(RESTART_WAIT)
            if await _attempt(arm, series, hold, attempt, retries, tries, sleep):
                return SeriesRun(arm, series.id, tuple(tries), attempt)
    except (HarnessFault, RunStopped) as stop:
        stop.tries = tuple(tries)
        raise
    return SeriesRun(arm, series.id, tuple(tries), None)


async def _attempt(
    arm: str, series: Series, hold: Hold[T], attempt: int, retries: int, tries: list[Try[T]],
    sleep: Callable[[float], Awaitable[None]],
) -> bool:
    """Hold the series once from its briefing; False if a meeting ran out of retries."""
    for meeting in series.meetings:
        failed = not_started = 0
        for meeting_try in itertools.count(1):
            where = _where(arm, series, meeting, attempt, meeting_try)
            try:
                held = await hold(meeting, attempt, meeting_try)
            except (HarnessFault, RunStopped):
                raise
            except Exception as exc:
                raise HarnessFault(f"{where}: {type(exc).__name__}: {exc}") from exc
            unplaced = [e for e in held.errors if error_kind(e) is ErrorKind.HARNESS]
            if unplaced:
                raise HarnessFault(f"{where}: calls failed with {', '.join(unplaced)}")
            tries.append(Try(meeting.id, attempt, meeting_try, held))
            if held.start_failed:
                not_started += 1
                if not_started > retries:
                    last = f"; the last: {held.start_error}" if held.start_error else ""
                    raise HarnessFault(
                        f"{where}: the deployment did not start in {not_started} tries{last}",
                    ) from held.start_error
            elif held.cut_short:
                failed += 1
                if failed > retries:
                    return False
            else:
                break
            await sleep(RETRY_WAITS[min(failed + not_started, len(RETRY_WAITS)) - 1])
    return True


def _where(arm: str, series: Series, meeting: Meeting, attempt: int, meeting_try: int) -> str:
    """Where a fault happened, as every HarnessFault names it."""
    return f"{arm}, {series.id}, {meeting.id}, attempt {attempt}, try {meeting_try}"


@dataclass(frozen=True)
class SeriesKept:
    """The scored series every arm's comparisons keep, and those dropped."""

    kept: tuple[str, ...]
    dropped: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return len(self.kept) >= MIN_SERIES


def series_kept(runs: Iterable[SeriesRun[Any]], scored: Sequence[str]) -> SeriesKept:
    """A series dropped in any arm is dropped from every arm's comparisons."""
    dropped = {r.series for r in runs if r.dropped}
    return SeriesKept(
        kept=tuple(s for s in scored if s not in dropped),
        dropped=tuple(s for s in scored if s in dropped),
    )


def arm_a_hold(
    client: LLMClient, panel: Panel, series: Series, *, log_path: Path, model: str = ARMS_MODEL,
) -> Hold[arm_a.ArmAReply]:
    """Arm A's meetings: its one call raises what failed, and the call log keeps it.

    Build it once per series: every try of both attempts logs to *log_path*,
    which must not exist yet, so no earlier run's calls are counted again.
    Each call names *model*. Only a provider error holds the meeting again;
    anything else the call raises is a harness fault, since the harness
    builds arm A's request.
    """
    if log_path.exists():
        raise FileExistsError(f"{log_path}: an earlier run's calls would be counted again")
    # The runtime drops a line it cannot write, and arm A's spend with it.
    log_path.parent.mkdir(parents=True, exist_ok=True)

    async def hold(meeting: Meeting, attempt: int, meeting_try: int) -> Held[arm_a.ArmAReply]:
        try:
            reply = await arm_a.run_meeting(
                client, panel, series, meeting, log_path=log_path,
                attempt=attempt, meeting_try=meeting_try, model=model,
            )
        except Exception as exc:
            if error_kind(type(exc).__name__) is not ErrorKind.PROVIDER:
                raise
            return Held(None, errors=(type(exc).__name__,))
        return Held(reply)

    return hold


def attempt_directory(root: Path, attempt: int) -> Path:
    """Where one attempt at a channel arm's series keeps its meetings' tries,
    and in arm D the attempt's deployment."""
    return root / f"attempt-{attempt}"


def try_directory(root: Path, attempt: int, meeting: Meeting, meeting_try: int) -> Path:
    """Where one try of a channel-arm meeting keeps its call log and record,
    and in arms B and C its deployment."""
    return attempt_directory(root, attempt) / meeting.id / f"try-{meeting_try}"


def channel_hold(
    panel: Panel,
    arm: str,
    series: Series,
    root: Path,
    *,
    run: Callable[..., Awaitable[channel_arm.ChannelMeeting]],
    watch_seconds: float = 5.0,
) -> Hold[channel_arm.ChannelMeeting]:
    """The channel arms: each try in its own directory under *root*. *run*
    holds one meeting there, as ``functools.partial(deployed_meeting.run_meeting,
    binary=...)`` does for arms B and C, each try on a deployment of its own;
    arm D's hold (:func:`evaluators.exp001.arm_d.arm_d_hold`) and arm D-prime's
    (:func:`evaluators.exp001.arm_d_prime.arm_d_prime_hold`) pass their own.
    Every *watch_seconds* while the meeting runs, the harness reads the try's
    call log and ends the try at the first failed call that means it is held
    again. *run* tells ``ended`` when the meeting is over; from then on the
    processes are stopping, and a call that fails meanwhile is read once they
    have stopped, so none is killed while it still writes. A summary that
    fails once the series' last meeting is over changes nothing any meeting
    shows, and counts in no arm."""
    root = root.resolve()  # the orchestrator runs in its deployment's directory

    async def hold(
        meeting: Meeting, attempt: int, meeting_try: int,
    ) -> Held[channel_arm.ChannelMeeting]:
        directory = try_directory(root, attempt, meeting, meeting_try)
        log = directory / deployed_meeting.CALL_LOG
        task = asyncio.current_task()
        assert task is not None
        watcher = asyncio.ensure_future(_watch(log, task, watch_seconds))
        over: list[dt.datetime] = []

        def ended(at: dt.datetime) -> None:
            watcher.cancel()
            over.append(at)

        result: channel_arm.ChannelMeeting | None = None
        try:
            result = await run(
                panel, arm, series, meeting, attempt=attempt, meeting_try=meeting_try,
                directory=directory, ended=ended,
            )
        except asyncio.CancelledError:
            # The watcher's own cancel ends the try; any other goes on up.
            if not (_fired(watcher) and task.uncancel() == 0):
                raise
        except StartError as exc:
            return Held(None, start_failed=True, start_error=exc)
        except OrchestratorError:
            # The orchestrator is part of the system under test: one that
            # exited is recorded, as an adviser that exits is.
            if "orchestrator" not in _exited(directory):
                raise
        finally:
            watcher.cancel()
        calls = read_call_log(log)
        failures = calls.failures
        if over and meeting == series.meetings[-1]:
            failures = tuple(
                dataclasses.replace(f, counts_in_arm=False)
                if f.purpose is CallPurpose.SUMMARY and f.started_at >= over[0] else f
                for f in failures
            )
        errors = _bearing(failures)
        if errors and not calls.records and all(error_kind(e) is ErrorKind.SYSTEM for e in errors):
            raise HarnessFault(
                f"{_where(arm, series, meeting, attempt, meeting_try)}: every call was refused "
                f"({', '.join(sorted(set(errors)))}), as a spend limit or an empty balance is",
            )
        return Held(result, errors=errors)

    return hold


def _bearing(failures: Iterable[FailedCall]) -> tuple[str, ...]:
    """The failed calls that bear on a meeting: every one of the arm's design,
    and any the harness cannot place. A memory write in an arm with no
    memory changes nothing the meeting shows, and neither does a keep-alive
    the provider failed or the runtime cut off: at worst the next turn
    writes the prefix again, which check 3 names."""
    return tuple(
        f.error for f in failures
        if (f.counts_in_arm and not _keepalive_lapse(f))
        or error_kind(f.error) is ErrorKind.HARNESS
    )


def _keepalive_lapse(failure: FailedCall) -> bool:
    return (
        failure.purpose is CallPurpose.KEEPALIVE
        and error_kind(failure.error) is ErrorKind.PROVIDER
    )


async def _watch(log: Path, task: asyncio.Task[Any], seconds: float) -> bool:
    """Cancel *task* at the first failed call in *log* that means the try is
    held again, or that the harness cannot place; True once it has."""
    while True:
        await asyncio.sleep(seconds)
        try:
            errors = _bearing(read_call_log(log).failures)
        except (ValueError, TypeError, OSError):  # a line half written: look again
            continue
        if any(error_kind(e) is not ErrorKind.SYSTEM for e in errors):
            task.cancel()
            return True


def _fired(watcher: asyncio.Future[bool]) -> bool:
    """Whether the watcher cancelled the try: it returns True only once it has."""
    return (
        watcher.done() and not watcher.cancelled() and watcher.exception() is None
        and watcher.result()
    )


def _exited(directory: Path) -> Mapping[str, Any]:
    """The processes a failed meeting's record says had exited; none without a record."""
    try:
        record = json.loads((directory / deployed_meeting.RECORD).read_text())
    except (OSError, ValueError):
        return {}
    exited = record.get("exited") if isinstance(record, dict) else None
    return exited if isinstance(exited, dict) else {}
