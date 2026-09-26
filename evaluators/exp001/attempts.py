"""Attempts and failures: what the harness does when a meeting goes wrong.

Pre-registration §3 sorts what can go wrong into three kinds, and this
module holds every arm's meetings by those rules.

- **A provider error**, such as a rate limit, a server error or a timeout, is
  retried up to three times: the meeting is held again, as its next try. A
  persona turn that ends in one publishes nothing, so it looks like silence,
  and the harness reads them from the call log instead, where a failed
  call's line names the exception's class. If the meeting still fails after
  three retries, the arm's series starts again from its briefing as its
  second attempt. If that fails too, the series is dropped from every arm's
  comparisons. With fewer than four series left, the run is incomplete.
- **A failure the system causes** is not retried: the meeting stands as it
  went. A request the provider refused as malformed is one, since the
  runtime built it; the channel arms record the others, such as a
  discussion that never closed or a missing memo.
- **A harness fault** stops the run: :class:`HarnessFault`. A wrong key or
  model, a deployment set up against the design (a rate limiter left on, a
  spending limit that refused a lease), an orchestrator or call log the
  harness cannot read, and any error it cannot place are harness faults;
  it never guesses.

A deployment that does not start is held again like a provider error: the
operator had not spoken, so nothing of the meeting happened. One that still
does not start after three retries is a harness fault.

Every try is kept, so a cut-short try's spend is reported; dollars per plan
count only the try that finished (:meth:`SeriesRun.finished_tries`).
"""

from __future__ import annotations

import enum
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, TypeVar

from agents.llm_client import LLMClient
from evaluators.exp001 import arm_a, channel_arm
from evaluators.exp001.deployment import StartError
from evaluators.exp001.materials import Meeting, Series
from evaluators.exp001.panel import Panel
from evaluators.exp001.runtime import CallLogError, read_call_log

T = TypeVar("T")

RETRIES = 3
ATTEMPTS = 2  # the first, and one start again from the briefing
MIN_SERIES = 4

# Exception class names, as the call log records them. The provider's SDK
# retries these twice itself before one reaches the log.
PROVIDER_ERRORS = frozenset({
    "RateLimitError",  # 429
    "InternalServerError",  # any 5xx, the 529 "overloaded" included
    "APITimeoutError",
    "APIConnectionError",
    "TimeoutError",
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
    reviewed PR (pre-registration §3)."""


@dataclass(frozen=True)
class Held(Generic[T]):
    """What one holding of a meeting gave.

    *result* is the arm's own record of the meeting, None when there is
    none: a deployment that did not start, or arm A's one call failing.
    *errors* names the exception class of every call that failed.
    """

    result: T | None
    errors: tuple[str, ...] = ()
    start_failed: bool = False

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
) -> SeriesRun[T]:
    """Hold every meeting of *series* for *arm*, by the rules above."""
    tries: list[Try[T]] = []
    for attempt in range(1, ATTEMPTS + 1):
        if await _attempt(arm, series, hold, attempt, retries, tries):
            return SeriesRun(arm, series.id, tuple(tries), attempt)
    return SeriesRun(arm, series.id, tuple(tries), None)


async def _attempt(
    arm: str, series: Series, hold: Hold[T], attempt: int, retries: int, tries: list[Try[T]],
) -> bool:
    """Hold the series once from its briefing; False if a meeting ran out of retries."""
    for meeting in series.meetings:
        held_here: list[Held[T]] = []
        for meeting_try in range(1, retries + 2):
            held = await hold(meeting, attempt, meeting_try)
            where = f"{arm}, {series.id}, {meeting.id}, attempt {attempt}, try {meeting_try}"
            unplaced = [e for e in held.errors if error_kind(e) is ErrorKind.HARNESS]
            if unplaced:
                raise HarnessFault(f"{where}: calls failed with {', '.join(unplaced)}")
            tries.append(Try(meeting.id, attempt, meeting_try, held))
            held_here.append(held)
            if not held.cut_short:
                break
        else:
            if all(h.start_failed for h in held_here):
                raise HarnessFault(f"{where}: the deployment did not start in {retries + 1} tries")
            return False
    return True


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
    client: LLMClient, panel: Panel, series: Series, *, log_path: Path,
) -> Hold[arm_a.ArmAReply]:
    """Arm A's meetings: its one call raises what failed, and the call log keeps it."""

    async def hold(meeting: Meeting, attempt: int, meeting_try: int) -> Held[arm_a.ArmAReply]:
        try:
            reply = await arm_a.run_meeting(
                client, panel, series, meeting, log_path=log_path,
                attempt=attempt, meeting_try=meeting_try,
            )
        except Exception as exc:
            name = type(exc).__name__
            if error_kind(name) is ErrorKind.HARNESS:
                raise HarnessFault(
                    f"A, {series.id}, {meeting.id}, attempt {attempt}, try {meeting_try}: "
                    f"{name}: {exc}",
                ) from exc
            return Held(None, errors=(name,))
        return Held(reply)

    return hold


def try_directory(root: Path, attempt: int, meeting: Meeting, meeting_try: int) -> Path:
    """Where one try of a channel-arm meeting keeps its deployment, call log and record."""
    return root / f"attempt-{attempt}" / meeting.id / f"try-{meeting_try}"


def channel_hold(
    panel: Panel,
    arm: str,
    series: Series,
    root: Path,
    *,
    run: Callable[..., Awaitable[channel_arm.ChannelMeeting]] = channel_arm.run_meeting,
    **options: Any,
) -> Hold[channel_arm.ChannelMeeting]:
    """Arms B and C: each try on a deployment of its own, in its own directory
    under *root*; *options* go to ``channel_arm.run_meeting``."""

    async def hold(
        meeting: Meeting, attempt: int, meeting_try: int,
    ) -> Held[channel_arm.ChannelMeeting]:
        directory = try_directory(root, attempt, meeting, meeting_try)
        where = f"{arm}, {series.id}, {meeting.id}, attempt {attempt}, try {meeting_try}"
        try:
            result = await run(
                panel, arm, series, meeting, attempt=attempt, meeting_try=meeting_try,
                directory=directory, **options,
            )
        except StartError:
            return Held(None, start_failed=True)
        except Exception as exc:
            raise HarnessFault(f"{where}: {type(exc).__name__}: {exc}") from exc
        try:
            failures = read_call_log(directory / channel_arm.CALL_LOG).failures
        except CallLogError as exc:
            raise HarnessFault(f"{where}: {exc}") from exc
        return Held(result, errors=tuple(f.error for f in failures))

    return hold
