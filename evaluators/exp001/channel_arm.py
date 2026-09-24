"""Arms B and C: one meeting in a channel, then the memo turn.

Each meeting of B and C runs on a new deployment of the four advisers, with
one channel whose members are the advisers and the operator. The operator
posts the meeting's message word for word, and the advisers discuss it until
the discussion closes, however it closes: an end vote; the round limit or the
depth cap, each after the chair's synthesis; the cost bound; or the idle
window. The harness reads each close from the orchestrator's log. An idle
close is logged only when the next message is posted, so a channel quiet for
the whole idle window has closed as well.

At every meeting but the briefing the memo turn follows. The harness disarms
the channel, makes the other three advisers observers, and posts the panel's
memo-turn instruction as the operator, mentioning only the chair. The memo is
the chair's first message after that request. With the channel disarmed and
nobody else able to answer, the memo turn starts no new discussion (check 5);
a message after the memo's request from anyone else, or a second one from the
chair, is recorded as a failure.

The harness does not wait forever. A discussion still open an hour after the
operator's message never closed: it is recorded, and the memo turn is held
anyway, which ends it. A chair that has not answered 35 minutes after the
request wrote no memo; that allows three of the provider library's 10-minute
tries. Both are failures the system causes. They are recorded, not retried,
and the meeting's result stands as it is.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import json
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
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
IDLE = "idle"


@dataclass(frozen=True)
class Limits:
    """How long the harness waits for each thing a meeting needs."""

    discussion: dt.timedelta = dt.timedelta(minutes=60)
    # The orchestrator's own idle window, which no channel here changes.
    idle: dt.timedelta = dt.timedelta(seconds=600)
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
    memo_turn: MemoTurn | None  # None at a briefing
    memo: Message | None  # None at a briefing, or when the memo is missing
    transcript: tuple[Message, ...]  # every message in the channel, oldest first
    failures: tuple[str, ...]


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
) -> ChannelMeeting:
    """Post the meeting's message, wait for the discussion to close, then hold the memo turn."""
    if len(meeting.message) > OPERATOR_MESSAGE_LIMIT:
        raise ValueError(
            f"{meeting.id}: the message is longer than the {OPERATOR_MESSAGE_LIMIT} characters "
            "an adviser reads",
        )
    wait = _Wait(room, channel, now, sleep, limits)
    opened = await room.post(channel, panel.operator, meeting.message)
    closed_at, trigger = await wait.for_close(read_log, opened)
    failures = [] if closed_at is not None else [NEVER_CLOSED]
    memo_turn = memo = request = None
    if meeting.kind is not MeetingKind.BRIEFING:
        await room.disarm(channel)
        for adviser in panel.advisers:
            if adviser.id != panel.chair.id:
                await room.set_respond(channel, adviser.id, "observer")
        asked_at = now()
        # The orchestrator stamps the request with the close it filed just
        # before it, an idle one included; the transcript keeps that stamp.
        request = await room.post(
            channel, panel.operator, panel.memo_turn_instruction(meeting.kind),
            mentions=[panel.chair.id],
        )
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
        opened=opened, closed_at=closed_at, trigger=trigger, memo_turn=memo_turn, memo=memo,
        transcript=transcript, failures=tuple(failures),
    )


def _went_on(transcript: Sequence[Message], request: Message, memo: Message | None) -> bool:
    """Whether anything but the memo was posted after the memo's request."""
    after = [m.id for m in transcript[[m.id for m in transcript].index(request.id) + 1:]]
    return after != ([memo.id] if memo is not None else [])


@dataclass
class _Wait:
    room: Room
    channel: str
    now: Callable[[], dt.datetime]
    sleep: Callable[[float], Awaitable[None]]
    limits: Limits

    async def for_close(
        self, read_log: Callable[[], OrchestratorLog], opened: Message,
    ) -> tuple[dt.datetime | None, str | None]:
        """When and how the discussion *opened* started closed; (None, None) if it never did."""
        while True:
            closes = [c for c in read_log().closes_in(self.channel) if c.at >= opened.at]
            if closes:
                return closes[0].at, closes[0].trigger
            heard = await self.room.messages(self.channel)
            last = max((m.at for m in heard), default=opened.at)
            if self.now() - last >= self.limits.idle:
                return last + self.limits.idle, IDLE
            if self.now() - opened.at >= self.limits.discussion:
                return None, None
            await self.sleep(self.limits.poll_seconds)

    async def for_reply(self, sender: str, request: Message) -> Message | None:
        """*sender*'s first message after *request*, or None once the memo limit has passed."""
        while True:
            heard = await self.room.messages(self.channel)
            after = heard[[m.id for m in heard].index(request.id) + 1:]
            reply = next((m for m in after if m.sender == sender), None)
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

    *directory* then holds the deployment, the call log its advisers write
    and the meeting's record. The deployment is stopped however the meeting
    ends. *room* stands in for the orchestrator's REST API in tests; by
    default the harness talks to the deployment's own.
    """
    if arm not in ARMS:
        raise ValueError(f"arm {arm} is not held on a new deployment per meeting; only {ARMS} are")
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

    async with _connect(room, ports) as api:
        try:
            await deployment.start(api)
            if not orchestrator_log().rate_limit_off:
                raise DeploymentError(
                    f"the orchestrator's rate limiter is on; see {layout.log('orchestrator')}",
                )
            result = await hold_meeting(
                api, orchestrator_log, panel, arm, series, meeting, channel=f"group:{name}",
                attempt=attempt, now=now, sleep=sleep, limits=limits,
            )
        finally:
            await deployment.stop()
    write_record(directory / RECORD, result)
    if refused := orchestrator_log().refused_leases:
        raise DeploymentError(
            "the wallet refused leases the deployment's limits should allow (check 6): "
            + "; ".join(refused),
        )
    return result


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
