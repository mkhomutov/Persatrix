"""Arm D′: arm C's meetings, with the series' earlier transcripts in a cached prefix.

Arm D′ asks whether long context can stand in for the memory allocator. Its
advisers meet as arm C's do, each meeting on a new deployment with empty
stores and a memory budget of 0 (:mod:`evaluators.exp001.deployed_meeting`).
What carries from one meeting to the next is text: wherever arm D's prompts
receive recalled memory, D′'s receive the full transcripts of the series'
earlier meetings, oldest first, placed before anything that changes between
calls and marked for the provider's cache (pre-registration §2). In the
runtime that is every call of an adviser's turn, the memo turn's included
(:mod:`agents.prompt_prefix`). The transcripts are those of each earlier
meeting's finished try in the same attempt, so the briefing carries none. A
meeting whose orchestrator exited before the harness could read its messages
is read from the orchestrator's own store, as far as it had stored them.

Check 3 asks that the first call of a meeting that carries the prefix writes
it to the cache, that every later call of the meeting reads it, and that no
other arm sets a cache breakpoint. Every call that carries it offers the
same tools, which the provider reads first, and then the same text, so the
four advisers share one cache entry. The shipped floor control gives one
adviser the floor at a time, and moves on after 45 seconds whether or not
that adviser's call has begun, so a meeting's first turn is under way before
the next begins only when it starts within those 45 seconds; a slower one,
held up by the provider, lets two turns write the prefix. An entry lives
five minutes after the last call that wrote or read it began. So a try that
carries a prefix waits until the entry the last try with the same prefix
left has gone, and its first turn writes the prefix again rather than
reading what an earlier try paid for. :func:`check_cache` reads check 3 from
the call records; the practice run shows it on the real provider.

An entry does not outlast a quiet spell, and a governed discussion can go
quiet by design: the chair gets one forced turn per discussion. A discussion
that closes by its 600-second idle window has made no call for longer than
an entry lives, so the memo turn after it writes the prefix again, and
:func:`check_cache` names that call.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, NamedTuple

from agents.call_log import prefix_sha256
from evaluators.exp001 import deployed_meeting
from evaluators.exp001.attempts import Held, Hold, channel_hold, try_directory
from evaluators.exp001.channel_arm import ChannelMeeting, channel_name
from evaluators.exp001.costs import CallPurpose, CallRecord
from evaluators.exp001.deployment import Layout
from evaluators.exp001.materials import Meeting, Series
from evaluators.exp001.orchestrator import Message
from evaluators.exp001.panel import Panel
from evaluators.exp001.runtime import FailedCall

__all__ = [
    "ARM",
    "CACHE_LIFETIME",
    "HEADING",
    "RETRY_GAP",
    "Posted",
    "TryKey",
    "arm_d_prime_hold",
    "check_cache",
    "prefix_sha256",
    "stored_transcript",
    "transcript_prefix",
]

ARM = "D-prime"
# A cache entry lives this long after the last call that wrote or read it
# began: the five-minute cache, whose write price is in the fixed table.
CACHE_LIFETIME = dt.timedelta(minutes=5)
# How long after a try that carried a prefix stopped the next try with the
# same prefix may begin, so the entry the first one left has gone.
RETRY_GAP = CACHE_LIFETIME + dt.timedelta(minutes=1)
HEADING = (
    "Transcripts of your earlier meetings with the same members, oldest first. "
    "Each holds every message posted in that meeting's channel, in the order "
    "they were posted."
)
# The calls that are an adviser speaking: its turns, and the chair's memo.
_SPEAKING = frozenset({CallPurpose.REPLY, CallPurpose.MEMO})

# One try of one meeting: arm, series, meeting, attempt and try.
TryKey = tuple[str, str, str, int, int]


class Posted(NamedTuple):
    """A message as a prefix shows it: its sender's ID, and its words."""

    sender: str
    content: str


def _real_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def transcript_prefix(meetings: Sequence[tuple[str, Sequence[Message | Posted]]]) -> str:
    """The prefix of a meeting whose earlier meetings are *meetings*, oldest
    first, each given as its channel's name and every message in it; empty
    when there are none. A message reads as the conversation window shows a
    peer's: its sender's ID in brackets, then its words as posted."""
    if not meetings:
        return ""
    parts = [HEADING]
    for channel, transcript in meetings:
        parts.append(f"Meeting in #{channel}")
        parts.extend(f"[{message.sender}]: {message.content}" for message in transcript)
    return "\n\n".join(parts) + "\n"


def stored_transcript(directory: Path, channel: str) -> tuple[Posted, ...]:
    """What the orchestrator of the try in *directory* had stored in
    *channel*, oldest first, read from its store without changing it; none
    when it stored nothing."""
    db = Layout(directory / "deployment").data / "channels.db"
    if not db.is_file():
        return ()
    with deployed_meeting.read_only(db) as store:
        rows = store.execute(
            "SELECT sender_id, content FROM messages WHERE channel_id = ? "
            "ORDER BY timestamp, rowid",
            (channel,),
        ).fetchall()
    return tuple(Posted(str(sender), str(content)) for sender, content in rows)


def arm_d_prime_hold(
    panel: Panel,
    series: Series,
    root: Path,
    *,
    binary: Path,
    watch_seconds: float = 5.0,
    now: Callable[[], dt.datetime] = _real_now,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    run: Callable[..., Awaitable[ChannelMeeting]] = deployed_meeting.run_meeting,
    **options: Any,
) -> Hold[ChannelMeeting]:
    """Arm D′'s meetings, held by the rules of attempts and failures as B's
    and C's are, each try on a deployment of its own under *root*.

    Each meeting carries the transcripts of the finished tries of the
    meetings before it in the same attempt. A meeting whose orchestrator
    exited is carried as far as its store holds it, and left out when it
    holds nothing. A try that carries a prefix begins no sooner than
    :data:`RETRY_GAP` after the last try with the same prefix stopped, unless
    that one never started; a try that carries none never waits. *run* holds
    one try, as :func:`evaluators.exp001.deployed_meeting.run_meeting` does,
    and is given *binary*, *now*, *sleep* and *options* as they are.
    """
    transcripts: dict[tuple[int, str], Sequence[Message | Posted]] = {}
    # When the last try that carried each prefix stopped, by the prefix's SHA-256.
    stopped: dict[str, dt.datetime] = {}
    tries = root.resolve()  # as channel_hold lays its tries out

    def prefix_of(meeting: Meeting, attempt: int) -> str:
        earlier = series.meetings[:series.meetings.index(meeting)]
        return transcript_prefix([
            (channel_name(series, m), transcripts[(attempt, m.id)])
            for m in earlier if (attempt, m.id) in transcripts
        ])

    async def run_try(
        _panel: Panel, _arm: str, _series: Series, meeting: Meeting, *,
        attempt: int, meeting_try: int, directory: Path, ended: Callable[[dt.datetime], None],
    ) -> ChannelMeeting:
        return await run(
            panel, ARM, series, meeting, attempt=attempt, meeting_try=meeting_try,
            directory=directory, ended=ended, prefix=prefix_of(meeting, attempt),
            binary=binary, now=now, sleep=sleep, **options,
        )

    inner = channel_hold(panel, ARM, series, root, run=run_try, watch_seconds=watch_seconds)

    async def hold(meeting: Meeting, attempt: int, meeting_try: int) -> Held[ChannelMeeting]:
        digest = prefix_sha256(prefix_of(meeting, attempt))
        last = stopped.get(digest) if digest is not None else None
        if last is not None:
            wait = (last + RETRY_GAP - now()).total_seconds()
            if wait > 0:
                await sleep(wait)
        held = await inner(meeting, attempt, meeting_try)
        if digest is not None and not held.start_failed:
            stopped[digest] = now()
        if not held.cut_short:
            transcript: Sequence[Message | Posted] = (
                held.result.transcript if held.result is not None else stored_transcript(
                    try_directory(tries, attempt, meeting, meeting_try),
                    f"group:{channel_name(series, meeting)}",
                )
            )
            if transcript:
                transcripts[(attempt, meeting.id)] = transcript
        return held

    return hold


def check_cache(
    records: Iterable[CallRecord],
    *,
    failures: Iterable[FailedCall] = (),
    written: Mapping[TryKey, str] | None = None,
) -> list[str]:
    """What in *records* and *failures* breaks check 3; an empty list when it holds.

    The calls are taken try by try, in the order they began. A call that
    answered without a prefix, in any arm, must neither write to the cache
    nor read from it, and no arm but D-prime may carry a prefix at all. A
    D-prime try was given a prefix when *written* names one for it, as a
    SHA-256, or when any of its calls carried one. Then every turn of it, the
    memo's included and failed ones too, must carry that one prefix,
    *written*'s when it names one; a try that made no turn call has nothing
    to show. The first call that answered with the prefix must write it and
    read nothing, unless a call that carried it failed before it, since a
    failed call can still have written the entry; every later one must read
    it and write nothing.
    """
    written = written or {}
    answered: dict[TryKey, list[CallRecord]] = defaultdict(list)
    failed: dict[TryKey, list[FailedCall]] = defaultdict(list)
    for record in records:
        answered[_key(record)].append(record)
    for failure in failures:
        failed[_key(failure)].append(failure)
    findings: list[str] = []
    for key in sorted(answered.keys() | failed.keys() | written.keys()):
        where = "{}, {}, {}, attempt {}, try {}: ".format(*key)
        calls = sorted(answered.get(key, ()), key=lambda r: r.started_at)
        every: list[CallRecord | FailedCall] = sorted(
            [*calls, *failed.get(key, ())], key=lambda c: c.started_at,
        )
        for call in every:
            if call.cache_prefix is not None and key[0] != ARM:
                findings.append(
                    where + f"{_call(call)} carried a prefix, which only arm {ARM}'s turns do",
                )
        for record in calls:
            if record.cache_prefix is None and (
                record.cache_write_tokens or record.cache_read_tokens
            ):
                findings.append(
                    where + f"{_call(record)} carried no prefix, yet {_touched(record)}",
                )
        if key[0] != ARM:
            continue
        prefixes = {call.cache_prefix for call in every if call.cache_prefix is not None}
        expected = written.get(key)
        if expected is not None or prefixes:
            findings.extend(
                where + f"{_call(call)} carried no prefix, though the try was given one"
                for call in every
                if call.purpose in _SPEAKING and call.cache_prefix is None
            )
        if len(prefixes) > 1:
            findings.append(
                where + f"the calls that carried a prefix carried {len(prefixes)} "
                "different prefixes",
            )
        if expected is not None and prefixes and prefixes != {expected}:
            findings.append(
                where + f"the calls carried {', '.join(sorted(_short(p) for p in prefixes))}, "
                f"not the prefix the harness wrote ({_short(expected)})",
            )
        carried = [record for record in calls if record.cache_prefix is not None]
        if not carried:
            continue
        first = carried[0]
        wrote = bool(first.cache_write_tokens) and not first.cache_read_tokens
        only_read = bool(first.cache_read_tokens) and not first.cache_write_tokens
        failed_first = any(
            f.cache_prefix is not None and f.started_at <= first.started_at
            for f in failed.get(key, ())
        )
        if not wrote and not (only_read and failed_first):
            findings.append(
                where + "the first call that carried the prefix wrote "
                f"{first.cache_write_tokens} tokens to the cache and read "
                f"{first.cache_read_tokens}; it should write the prefix and read nothing"
                + (
                    ". A read with nothing written means an earlier request wrote the "
                    "entry: perhaps a failed attempt the provider's library retried inside "
                    "this call, which only the provider's usage report shows"
                    if only_read else ""
                ),
            )
        for before, record in zip(carried, carried[1:], strict=False):
            if not record.cache_read_tokens or record.cache_write_tokens:
                gap = (record.started_at - before.started_at).total_seconds()
                findings.append(
                    where + f"{_call(record)} {_touched(record)}, {gap:g} s after the call "
                    "before it that carried the prefix began; it should read the prefix "
                    "and write nothing",
                )
    return findings


def _key(call: CallRecord | FailedCall) -> TryKey:
    return (call.arm, call.series, call.meeting, call.attempt, call.meeting_try)


def _call(call: CallRecord | FailedCall) -> str:
    who = call.adviser if call.adviser is not None else f"arm {call.arm}"
    return f"{who}'s {call.purpose.value} call at {call.started_at.isoformat()}"


def _touched(call: CallRecord) -> str:
    return f"wrote {call.cache_write_tokens} and read {call.cache_read_tokens} tokens of the cache"


def _short(digest: str) -> str:
    return f"{digest[:12]}…"
