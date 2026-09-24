"""Arms B and C: one meeting in a channel, then the memo turn.

Each meeting of B and C runs on a new deployment of the four advisers, with
one channel whose members are the advisers and the operator. The operator
posts the meeting's message word for word, and the advisers discuss it until
the discussion closes, however it closes: an end vote; the round limit or the
depth cap, each after the chair's synthesis; the cost bound; or the idle
window. The harness reads each close from the orchestrator's log. An idle
close is logged only when the next message is posted, so a channel quiet for
the whole idle window has closed as well, and so has one whose messages were
ever that far apart.

At every meeting but the briefing the memo turn follows. A turn the floor
sent before the close can still post, so the harness first waits until no
adviser has one in flight. Then it disarms the channel, makes the other three
advisers observers, and posts the panel's memo-turn instruction as the
operator, mentioning only the chair. The memo is the chair's first message
after that request, leaving out a closing synthesis that ran late. With the
channel disarmed and nobody else able to answer, the memo turn starts no new
discussion (check 5); a message after the memo's request from anyone else, or
a second one from the chair, is recorded as a failure.

The harness does not wait forever. A discussion still open an hour after the
operator's message never closed: it is recorded, and no memo turn is held, so
the meeting has no memo. A chair that has not answered 35 minutes after the
request wrote no memo: a persona turn ends at the runtime's 300-second event
timeout, and a turn that runs out posts nothing, so 35 minutes covers the
memo turn and six turns queued ahead of it. Both are failures the system
causes. They are recorded, not retried, and the meeting's result stands as it
is.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import datetime as dt
import itertools
import json
import signal
import sqlite3
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import aiohttp

from evaluators.exp001.deployment import (
    ARMS_ALIAS,
    REPO,
    Alias,
    DeploymentError,
    Layout,
    channel_config,
    write_deployment,
)
from evaluators.exp001.materials import Meeting, MeetingKind, Series
from evaluators.exp001.orchestrator import (
    Close,
    Message,
    Orchestrator,
    OrchestratorLog,
    read_orchestrator_log,
)
from evaluators.exp001.panel import Panel
from evaluators.exp001.processes import (
    LOOPBACK,
    Deployment,
    Handle,
    Ports,
    Process,
    Registry,
    adviser_process,
    free_ports,
    launch,
    orchestrator_process,
)
from evaluators.exp001.runtime import MemoTurn, call_log_env, meeting_clock_env

# The arms this module holds; each of their meetings gets a new deployment.
ARMS = ("B", "C")
# What a meeting's directory holds besides its deployment.
CALL_LOG = "calls.jsonl"
RECORD = "meeting.json"
# An adviser drops an incoming message longer than this, and says nothing.
OPERATOR_MESSAGE_LIMIT = 4000
NEVER_CLOSED = "never_closed"
MISSING_MEMO = "missing_memo"
MEMO_TURN_WENT_ON = "memo_turn_went_on"
# The memo request carries the orchestrator's own record of the close before
# it, and that close is not the one the harness read.
CLOSE_MISMATCH = "close_mismatch"
# A process of the deployment exited before the harness stopped it.
PROCESS_EXITED = "process_exited"
# The wallet refused a lease for a reason other than a spending limit.
LEASE_REFUSED = "lease_refused"
IDLE = "idle"


@dataclass(frozen=True)
class Limits:
    """How long the harness waits for each thing a meeting needs."""

    discussion: dt.timedelta = dt.timedelta(minutes=60)
    # The orchestrator's own idle window, which no channel here changes.
    idle: dt.timedelta = dt.timedelta(seconds=600)
    # For turns sent before the close to post; the orchestrator stops
    # counting one that never does 90 seconds after it was sent.
    drain: dt.timedelta = dt.timedelta(minutes=5)
    # The memo turn and six turns queued ahead of it, 300 seconds each.
    memo: dt.timedelta = dt.timedelta(minutes=35)
    # After the memo, so a message the memo turn set off is seen.
    settle: dt.timedelta = dt.timedelta(seconds=10)
    poll_seconds: float = 5.0


LIMITS = Limits()


class Room(Protocol):
    """What holding a meeting asks of the orchestrator."""

    async def post(
        self, channel: str, sender: str, content: str, *, mentions: Sequence[str] = (),
    ) -> Message: ...

    async def messages(self, channel: str) -> list[Message]: ...

    async def activity(self, channel: str) -> set[str]: ...

    async def disarm(self, channel: str) -> None: ...

    async def set_respond(self, channel: str, member: str, respond: str) -> None: ...


@dataclass(frozen=True)
class ChannelMeeting:
    """One meeting of a channel arm as it went; every time is real time."""

    arm: str
    series: str
    meeting: str
    attempt: int
    channel: str
    opened: Message  # the operator's message, as stored
    closed_at: dt.datetime | None  # None: the discussion never closed
    trigger: str | None  # end_votes, structural, cost or idle
    closed_by: str | None  # as part 2 reports it: vote, depth_cap, round_limit, cost or idle
    memo_turn: MemoTurn | None  # None at a briefing, or when the discussion never closed
    # Each adviser's energy as the memo was asked for, None if its store had
    # saved none yet; empty when there was no memo turn.
    energy: Mapping[str, float | None]
    memo: Message | None  # None at a briefing, or when the memo is missing
    transcript: tuple[Message, ...]  # every message in the channel, oldest first
    failures: tuple[str, ...]
    # The processes that exited before the harness stopped them, with their codes.
    exited: Mapping[str, int] = dataclasses.field(default_factory=dict)


def channel_name(series: Series, meeting: Meeting) -> str:
    """The meeting's channel, named by its place in the series, the same in every arm."""
    return f"advice-{series.meetings.index(meeting) + 1}"


def _real_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


async def hold_meeting(
    room: Room,
    read_log: Callable[[], OrchestratorLog],
    panel: Panel,
    arm: str,
    series: Series,
    meeting: Meeting,
    *,
    channel: str,
    attempt: int,
    now: Callable[[], dt.datetime] = _real_now,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    limits: Limits = LIMITS,
    read_energy: Callable[[], Mapping[str, float | None]] = dict,
) -> ChannelMeeting:
    """Post the meeting's message, wait for the discussion to close, then hold the memo turn.

    *read_energy* gives each adviser's energy, read as the memo is asked for.
    """
    if len(meeting.message) > OPERATOR_MESSAGE_LIMIT:
        raise ValueError(
            f"{meeting.id}: the message is longer than the {OPERATOR_MESSAGE_LIMIT} characters "
            "an adviser reads",
        )
    wait = _Wait(room, channel, now, sleep, limits)
    opened = await room.post(channel, panel.operator, meeting.message)
    closed_at, trigger, closed_by = await wait.for_close(read_log, opened)
    failures = [] if closed_at is not None else [NEVER_CLOSED]
    memo_turn = memo = request = None
    energy: Mapping[str, float | None] = {}
    if closed_at is not None:
        # A turn the floor sent before the close still posts, and lands while
        # the channel is armed, filed under the discussion that closed.
        await wait.for_drain()
    # A discussion that never closed gets no memo turn: asked now, the chair's
    # next discussion turn would pass for the memo.
    if closed_at is not None and meeting.kind is not MeetingKind.BRIEFING:
        await room.disarm(channel)
        for adviser in panel.advisers:
            if adviser.id != panel.chair.id:
                await room.set_respond(channel, adviser.id, "observer")
        energy = dict(read_energy())
        asked_at = now()
        # The orchestrator stamps the request with the close it filed just
        # before it, an idle one included; the transcript keeps that stamp.
        request = await room.post(
            channel, panel.operator, panel.memo_turn_instruction(meeting.kind),
            mentions=[panel.chair.id],
        )
        if request.closed_before is not None and request.closed_before[1] != trigger:
            failures.append(CLOSE_MISMATCH)
        memo_turn = MemoTurn(
            arm=arm, series=series.id, meeting=meeting.id, attempt=attempt,
            chair=panel.chair.id, asked_at=asked_at,
        )
        memo = await wait.for_reply(panel.chair.id, request)
        if memo is None:
            failures.append(MISSING_MEMO)
        else:
            await sleep(limits.settle.total_seconds())
    transcript = tuple(await room.messages(channel))
    if request is not None and _went_on(transcript, request, memo):
        failures.append(MEMO_TURN_WENT_ON)
    return ChannelMeeting(
        arm=arm, series=series.id, meeting=meeting.id, attempt=attempt, channel=channel,
        opened=opened, closed_at=closed_at, trigger=trigger, closed_by=closed_by,
        memo_turn=memo_turn, energy=energy, memo=memo, transcript=transcript,
        failures=tuple(failures),
    )


def _went_on(transcript: Sequence[Message], request: Message, memo: Message | None) -> bool:
    """Whether anything but the memo was posted after the memo's request."""
    after = [
        m.id for m in transcript[[m.id for m in transcript].index(request.id) + 1:]
        if not _late_synthesis(m)
    ]
    return after != ([memo.id] if memo is not None else [])


def _late_synthesis(message: Message) -> bool:
    """A closing synthesis posted after the memo's request: the discussion's
    last word, running late, not the memo turn's."""
    return message.metadata.get("synthesis_reply") is True


def _closed_by(close: Close, depth_capped: frozenset[str]) -> str:
    """What closed a discussion, as part 2 reports it; the orchestrator logs
    the depth cap and the round limit alike, as a structural close."""
    if close.trigger == "structural":
        return "depth_cap" if close.interaction in depth_capped else "round_limit"
    return {"end_votes": "vote"}.get(close.trigger, close.trigger)


@dataclass
class _Wait:
    room: Room
    channel: str
    now: Callable[[], dt.datetime]
    sleep: Callable[[float], Awaitable[None]]
    limits: Limits

    async def for_close(
        self, read_log: Callable[[], OrchestratorLog], opened: Message,
    ) -> tuple[dt.datetime | None, str | None, str | None]:
        """When and how the discussion *opened* started closed, and what closed
        it as part 2 reports it; all None if it never did."""
        while True:
            log = read_log()
            closes = [c for c in log.closes_in(self.channel) if c.at >= opened.at]
            idle_at = self._idle_close(opened, await self.room.messages(self.channel))
            if closes and (idle_at is None or closes[0].at <= idle_at):
                return closes[0].at, closes[0].trigger, _closed_by(closes[0], log.depth_capped)
            if idle_at is not None:
                return idle_at, IDLE, IDLE
            if self.now() - opened.at >= self.limits.discussion:
                return None, None, None
            await self.sleep(self.limits.poll_seconds)

    def _idle_close(self, opened: Message, heard: Sequence[Message]) -> dt.datetime | None:
        """The end of the first idle window since *opened*: the orchestrator
        closes an idle discussion when the next message comes, however late
        the harness looks."""
        times = sorted({opened.at, *(m.at for m in heard if m.at >= opened.at)})
        for before, after in itertools.pairwise([*times, self.now()]):
            if after - before >= self.limits.idle:
                return before + self.limits.idle
        return None

    async def for_drain(self) -> None:
        """Until no member has a turn in flight, or the drain limit has passed."""
        deadline = self.now() + self.limits.drain
        while await self.room.activity(self.channel) and self.now() < deadline:
            await self.sleep(self.limits.poll_seconds)

    async def for_reply(self, sender: str, request: Message) -> Message | None:
        """*sender*'s first message after *request*, or None once the memo limit has passed."""
        while True:
            heard = await self.room.messages(self.channel)
            after = heard[[m.id for m in heard].index(request.id) + 1:]
            reply = next(
                (m for m in after if m.sender == sender and not _late_synthesis(m)), None,
            )
            if reply is not None:
                return reply
            if self.now() - request.at >= self.limits.memo:
                return None
            await self.sleep(self.limits.poll_seconds)


class _Orchestrator(Room, Registry, Protocol):
    """The whole of what a meeting asks the orchestrator."""


async def run_meeting(
    panel: Panel,
    arm: str,
    series: Series,
    meeting: Meeting,
    *,
    attempt: int,
    directory: Path,
    binary: Path,
    python: Path = Path(sys.executable),
    repo: Path = REPO,
    alias: Alias = ARMS_ALIAS,
    spawn: Callable[[Process, Mapping[str, str]], Handle] = launch,
    room: _Orchestrator | None = None,
    now: Callable[[], dt.datetime] = _real_now,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    limits: Limits = LIMITS,
) -> ChannelMeeting:
    """Hold one meeting of arm B or C on a new deployment in *directory*.

    *directory* must be empty; it then holds the deployment, the call log
    its advisers write and the meeting's record, which a meeting that fails
    midway still leaves, naming the error. The deployment is stopped however
    the meeting ends, a hangup included. *room* stands in for the
    orchestrator's REST API in tests; by default the harness talks to the
    deployment's own.
    """
    if arm not in ARMS:
        raise ValueError(f"arm {arm} is not held on a new deployment per meeting; only {ARMS} are")
    # An earlier run's call log would be appended to, and counted again.
    if directory.exists() and any(directory.iterdir()):
        raise DeploymentError(f"{directory} is not empty; each meeting has a directory of its own")
    layout = Layout(directory / "deployment")
    name = channel_name(series, meeting)
    entry = channel_config(panel, arm, name=name, organisation=series.organisation)
    write_deployment(layout, panel, arm, channels=[entry], alias=alias)
    ports = free_ports()
    env = {
        **meeting_clock_env(meeting.story_date, now()),
        **call_log_env(
            directory / CALL_LOG, arm=arm, series=series.id, meeting=meeting.id,
            meeting_kind=meeting.kind, attempt=attempt,
        ),
    }
    deployment = Deployment(
        orchestrator_process(layout, ports, binary=binary),
        [adviser_process(layout, a.id, ports, python=python, repo=repo, env=env)
         for a in panel.advisers],
        spawn=spawn, clock=lambda: now().timestamp(), sleep=sleep,
    )

    def orchestrator_log() -> OrchestratorLog:
        return read_orchestrator_log(layout.log("orchestrator"))

    def energy() -> dict[str, float | None]:
        return {a.id: _energy(layout.memory_db(a.id), a.id) for a in panel.advisers}

    channel = f"group:{name}"
    with _cancelled_on_hangup():
        async with _connect(room, ports) as api:
            try:
                await deployment.start(api)
                started = orchestrator_log()
                if not started.rate_limit_off:
                    raise DeploymentError(
                        f"the orchestrator's rate limiter is on; see {layout.log('orchestrator')}",
                    )
                if not started.wallet_served:
                    raise DeploymentError(
                        f"the orchestrator serves no wallet; see {layout.log('orchestrator')}",
                    )
                result = await hold_meeting(
                    api, orchestrator_log, panel, arm, series, meeting, channel=channel,
                    attempt=attempt, now=now, sleep=sleep, limits=limits, read_energy=energy,
                )
                exited = deployment.exited()
            except Exception as exc:
                _write_failure(
                    directory / RECORD, error=exc, arm=arm, series=series.id,
                    meeting=meeting.id, attempt=attempt, channel=channel,
                    exited=deployment.exited(),
                )
                raise
            finally:
                try:
                    await deployment.stop()
                except BaseException:  # interrupted again: nothing may be left running
                    deployment.kill()
                    raise
    log = orchestrator_log()
    failures = [*result.failures]
    if exited:
        failures.append(PROCESS_EXITED)
    if log.refused_leases:
        failures.append(LEASE_REFUSED)
    result = dataclasses.replace(result, failures=tuple(failures), exited=exited)
    write_record(directory / RECORD, result)
    if log.spending_limit_refusals:
        raise DeploymentError(
            "a spending limit the deployment turns off refused leases (check 6): "
            + "; ".join(log.spending_limit_refusals),
        )
    return result


@contextlib.contextmanager
def _cancelled_on_hangup() -> Iterator[None]:
    """A hangup or SIGTERM cancels the meeting as Ctrl-C does, so its
    deployment, which the terminal's own signals never reach, is still stopped."""
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    installed: list[signal.Signals] = []
    if task is not None:
        for sig in (signal.SIGHUP, signal.SIGTERM):
            with contextlib.suppress(ValueError, RuntimeError):  # the main thread's alone
                loop.add_signal_handler(sig, task.cancel)
                installed.append(sig)
    try:
        yield
    finally:
        for sig in installed:
            loop.remove_signal_handler(sig)


def _energy(db: Path, adviser_id: str) -> float | None:
    """The energy an adviser's store last saved for it; None before it saved any."""
    try:
        with contextlib.closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True)) as store:
            row = store.execute(
                "SELECT persona_state_json FROM agent_state WHERE agent_id = ?", (adviser_id,),
            ).fetchone()
    except sqlite3.Error:
        return None
    energy = json.loads(row[0]).get("energy") if row is not None and row[0] else None
    return None if energy is None else float(energy)


@asynccontextmanager
async def _connect(room: _Orchestrator | None, ports: Ports) -> AsyncIterator[_Orchestrator]:
    if room is not None:
        yield room
        return
    async with aiohttp.ClientSession() as session:
        yield Orchestrator(f"http://{LOOPBACK}:{ports.http}", session)


def write_record(path: Path, meeting: ChannelMeeting) -> None:
    """Keep *meeting* as JSON: every field, times in ISO 8601."""
    path.write_text(json.dumps(_plain(meeting), indent=1, ensure_ascii=False))


def _write_failure(path: Path, *, error: BaseException, **known: Any) -> None:
    """Keep what is known of a meeting that failed midway, and why it failed."""
    record = {**_plain(known), "error": f"{type(error).__name__}: {error}"}
    path.write_text(json.dumps(record, indent=1, ensure_ascii=False))


def _plain(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    return value
