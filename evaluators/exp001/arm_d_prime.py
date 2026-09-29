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
meeting's finished try in the same attempt, so the briefing carries none.

Check 3 asks that the first call of a meeting that carries the prefix writes
it to the cache, that every later call of the meeting reads it, and that no
other arm sets a cache breakpoint. Every call that carries it offers the
same tools, which the provider reads first, and then the same text, so the
four advisers share one cache entry; and the shipped floor control gives one
adviser the floor at a time, so a meeting's first turn is under way before
the next begins, as a read needs. An entry lives five minutes after the last
call that wrote or read it began.
So a try held again waits until the entry the try before it left has gone,
and its first turn writes the prefix again rather than reading what a
discarded try paid for. :func:`check_cache` reads check 3 from the call
records; the practice run shows it on the real provider.

An entry does not outlast a quiet spell. A discussion that closes by its
600-second idle window has made no call for longer than an entry lives, so
the memo turn after it writes the prefix again, and :func:`check_cache`
names that call.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from evaluators.exp001 import deployed_meeting
from evaluators.exp001.attempts import Held, Hold, channel_hold
from evaluators.exp001.channel_arm import ChannelMeeting, channel_name
from evaluators.exp001.costs import CallRecord
from evaluators.exp001.materials import Meeting, Series
from evaluators.exp001.orchestrator import Message
from evaluators.exp001.panel import Panel

ARM = "D-prime"
# A cache entry lives this long after the last call that wrote or read it
# began: the five-minute cache, whose write price is in the fixed table.
CACHE_LIFETIME = dt.timedelta(minutes=5)
# How long after a try stopped the meeting's next try may begin, so the
# entry the try left has gone.
RETRY_GAP = CACHE_LIFETIME + dt.timedelta(minutes=1)
HEADING = (
    "Transcripts of your earlier meetings with the same members, oldest first. "
    "Each holds every message posted in that meeting's channel, in the order "
    "they were posted."
)

# One try of one meeting: arm, series, meeting, attempt and try.
TryKey = tuple[str, str, str, int, int]


def _real_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def transcript_prefix(meetings: Sequence[tuple[str, Sequence[Message]]]) -> str:
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


def prefix_sha256(prefix: str) -> str:
    """The prefix's SHA-256, as the call log names it."""
    return hashlib.sha256(prefix.encode("utf-8")).hexdigest()


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
    meetings before it in the same attempt. An earlier meeting that left no
    transcript, since its orchestrator exited, is left out. A try held again
    begins no sooner than :data:`RETRY_GAP` after the try before it stopped,
    unless that one never started. *run* holds one try, as
    :func:`evaluators.exp001.deployed_meeting.run_meeting` does, and is
    given *binary*, *now*, *sleep* and *options* as they are.
    """
    transcripts: dict[tuple[int, str], tuple[Message, ...]] = {}
    stopped: dict[tuple[int, str], dt.datetime] = {}

    async def run_try(
        _panel: Panel, _arm: str, _series: Series, meeting: Meeting, *,
        attempt: int, meeting_try: int, directory: Path, ended: Callable[[dt.datetime], None],
    ) -> ChannelMeeting:
        earlier = series.meetings[:series.meetings.index(meeting)]
        prefix = transcript_prefix([
            (channel_name(series, m), transcripts[(attempt, m.id)])
            for m in earlier if (attempt, m.id) in transcripts
        ])
        return await run(
            panel, ARM, series, meeting, attempt=attempt, meeting_try=meeting_try,
            directory=directory, ended=ended, prefix=prefix, binary=binary, now=now,
            sleep=sleep, **options,
        )

    inner = channel_hold(panel, ARM, series, root, run=run_try, watch_seconds=watch_seconds)

    async def hold(meeting: Meeting, attempt: int, meeting_try: int) -> Held[ChannelMeeting]:
        last = stopped.get((attempt, meeting.id))
        if last is not None:
            wait = (last + RETRY_GAP - now()).total_seconds()
            if wait > 0:
                await sleep(wait)
        held = await inner(meeting, attempt, meeting_try)
        if not held.start_failed:
            stopped[(attempt, meeting.id)] = now()
        if held.result is not None and not held.cut_short:
            transcripts[(attempt, meeting.id)] = held.result.transcript
        return held

    return hold


def check_cache(
    records: Iterable[CallRecord], *, written: Mapping[TryKey, str] | None = None,
) -> list[str]:
    """What in *records* breaks check 3; an empty list when it holds.

    The calls are taken try by try, in the order they began. In arm D-prime
    the first call that carried the prefix must write it to the cache and
    read nothing, and every later one must read it and write nothing, all of
    them carrying the one prefix: the one *written* names for the try, as a
    SHA-256, when it names one. A call that carried no prefix, in any arm,
    must neither write to the cache nor read from it, and no other arm may
    carry a prefix at all.
    """
    written = written or {}
    tries: dict[TryKey, list[CallRecord]] = defaultdict(list)
    for record in records:
        key = (record.arm, record.series, record.meeting, record.attempt, record.meeting_try)
        tries[key].append(record)
    for key in written:
        tries.setdefault(key, [])
    findings: list[str] = []
    for key in sorted(tries):
        where = "{}, {}, {}, attempt {}, try {}: ".format(*key)
        calls = sorted(tries[key], key=lambda r: r.started_at)
        for call in calls:
            if call.cache_prefix is None and (call.cache_write_tokens or call.cache_read_tokens):
                findings.append(where + f"{_call(call)} carried no prefix, yet {_touched(call)}")
            elif call.cache_prefix is not None and key[0] != ARM:
                findings.append(
                    where + f"{_call(call)} carried a prefix, which only arm {ARM}'s turns do",
                )
        carried = [call for call in calls if call.cache_prefix is not None]
        expected = written.get(key)
        if key[0] != ARM:
            continue
        if not carried:
            if expected is not None:
                findings.append(where + "no call carried the prefix the harness wrote")
            continue
        prefixes = {call.cache_prefix for call in carried}
        if len(prefixes) > 1:
            findings.append(
                where + f"the calls that carried a prefix carried {len(prefixes)} "
                "different prefixes",
            )
        if expected is not None and prefixes != {expected}:
            findings.append(
                where + f"the calls carried {', '.join(sorted(_short(p) for p in prefixes))}, "
                f"not the prefix the harness wrote ({_short(expected)})",
            )
        first = carried[0]
        if not first.cache_write_tokens or first.cache_read_tokens:
            findings.append(
                where + "the first call that carried the prefix wrote "
                f"{first.cache_write_tokens} tokens to the cache and read "
                f"{first.cache_read_tokens}; it should write the prefix and read nothing",
            )
        for before, call in zip(carried, carried[1:], strict=False):
            if not call.cache_read_tokens or call.cache_write_tokens:
                gap = (call.started_at - before.started_at).total_seconds()
                findings.append(
                    where + f"{_call(call)} {_touched(call)}, {gap:g} s after the call "
                    "before it that carried the prefix began; it should read the prefix "
                    "and write nothing",
                )
    return findings


def _call(call: CallRecord) -> str:
    who = call.adviser if call.adviser is not None else f"arm {call.arm}"
    return f"{who}'s {call.purpose.value} call at {call.started_at.isoformat()}"


def _touched(call: CallRecord) -> str:
    return f"wrote {call.cache_write_tokens} and read {call.cache_read_tokens} tokens of the cache"


def _short(digest: str | None) -> str:
    return f"{(digest or '')[:12]}…"
