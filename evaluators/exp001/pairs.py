"""One arm's series, held in a directory of its own, each try kept.

The run holds the arms' series one at a time. Each arm's series, a **pair**
of one arm and one series, is held in a directory of its own by the rules
of attempts and failures (:func:`evaluators.exp001.attempts.run_series`),
with the hold its arm needs (:func:`arm_hold`).

- **Each try is kept** as it ends, as a line of ``tries.jsonl``, on the
  disk before the next try begins: the meeting, the attempt and try,
  whether it started, the failed calls that bear on it, and the arm's whole
  record of the meeting, so arm A's replies, which no other file keeps, are
  kept too. The try's **answer**, the memo or the recall check's answers as
  the chair or arm A wrote them, is read from that record each time the
  pair is read, so the rule for a missing answer is applied in one place.
  There is none when it is missing, or at a briefing, which asks for none.
- **A pair held to the end** is marked finished in ``pair.json``, with the
  attempt that finished, or none when the series was dropped. A run started
  again reads it back, and never holds it again.
- **A pair stopped partway**, by Ctrl-C, a hangup or a crash, has no such
  mark. A run started again sets its directory aside whole and holds the
  series again from its briefing in a new one, so no pair mixes two
  holdings; the calls it made stay on disk.

A harness fault stops the pair where it is, with the tries before it kept.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import functools
import itertools
import json
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agents.llm_client import LLMClient
from agents.llm_types import StopReason
from evaluators.exp001 import arm_a, arm_d, arm_d_prime, attempts, deployed_meeting
from evaluators.exp001.arm_a import ArmAReply
from evaluators.exp001.attempts import Held, Hold, SeriesRun, Try, run_series
from evaluators.exp001.costs import ARMS
from evaluators.exp001.deployed_meeting import CALL_LOG, plain
from evaluators.exp001.deployment import Alias
from evaluators.exp001.files import append_line, write_json
from evaluators.exp001.materials import Meeting, MeetingKind, Series
from evaluators.exp001.panel import Panel
from evaluators.exp001.runtime import CallLog, MemoTurn, read_call_log

TRIES = "tries.jsonl"
PAIR = "pair.json"


@dataclass(frozen=True)
class Kept:
    """A try's result as the run keeps it: its answer, and the arm's whole record."""

    answer: str | None  # the memo, or the recall check's answers; None when missing
    record: Mapping[str, Any]  # the arm's record of the meeting, as JSON


def arm_hold(
    arm: str,
    panel: Panel,
    series: Series,
    directory: Path,
    *,
    client: LLMClient,
    binary: Path,
    alias: Alias,
) -> Hold[Any]:
    """How *arm*'s meetings of *series* are held, each try under *directory*.

    Arm A makes its one call a meeting through *client*, asking for *alias*'s
    model, and logs every try of the series to one new file there. The
    channel arms run *binary*, the orchestrator, with every model alias on
    *alias*: arms B and C hold each try on a new deployment, and arms D and
    D-prime by their own holds. *alias* is never a default, since the arms'
    own is the real provider's.
    """
    if arm == arm_a.ARM:
        return attempts.arm_a_hold(
            client, panel, series, log_path=directory / CALL_LOG, model=alias.model,
        )
    if arm in ("B", "C"):
        run = functools.partial(deployed_meeting.run_meeting, binary=binary, alias=alias)
        return attempts.channel_hold(panel, arm, series, directory, run=run)
    if arm == "D":
        return arm_d.arm_d_hold(panel, series, directory, binary=binary, alias=alias)
    if arm == "D-prime":
        return arm_d_prime.arm_d_prime_hold(panel, series, directory, binary=binary, alias=alias)
    raise ValueError(f"unknown arm {arm!r}; expected one of {ARMS}")


async def hold_pair(
    arm: str,
    series: Series,
    directory: Path,
    make_hold: Callable[[Path], Hold[Any]],
    *,
    interrupted: Path,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    progress: Callable[[str], None] | None = None,
) -> SeriesRun[Kept]:
    """Hold *series* for *arm* in *directory*, keeping each try as it ends.

    A pair already held there is read back, not held again. One stopped
    partway is moved into *interrupted*, as ``<series>-<arm>-N``, and held
    again from its briefing. *make_hold* builds the hold for the directory
    the pair is held in, as :func:`arm_hold` does. *progress* is told as
    each try begins and ends.
    """
    held_before = read_pair(directory, series)
    if held_before is not None:
        return held_before
    if directory.exists() and any(directory.iterdir()):
        _set_aside(directory, interrupted, f"{series.id}-{arm}")
    directory.mkdir(parents=True, exist_ok=True)
    hold = make_hold(directory)
    say = progress or (lambda _: None)

    async def kept(meeting: Meeting, attempt: int, meeting_try: int) -> Held[Any]:
        where = attempts.where(arm, series, meeting, attempt, meeting_try)
        say(f"{where}: holding")
        held = await hold(meeting, attempt, meeting_try)
        append_line(directory / TRIES, _line(arm, series, meeting, attempt, meeting_try, held))
        say(f"{where}: {_outcome(held)}")
        return held

    run = await run_series(arm, series, kept, sleep=sleep)
    write_json(directory / PAIR, {
        "arm": arm, "series": series.id, "finished_attempt": run.finished_attempt,
    })
    pair = read_pair(directory, series)
    assert pair is not None  # just marked
    return pair


def read_pair(directory: Path, series: Series) -> SeriesRun[Kept] | None:
    """The pair of *series* held in *directory*, as kept, each try's answer
    read from its record; None unless it was held to the end."""
    try:
        marked = json.loads((directory / PAIR).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    meetings = {m.id: m for m in series.meetings}
    tries = tuple(
        _try(line, meetings[line["meeting"]], marked["arm"]) for line in _lines(directory / TRIES)
    )
    return SeriesRun(marked["arm"], marked["series"], tries, marked["finished_attempt"])


def pair_calls(directory: Path, run: SeriesRun[Kept]) -> CallLog:
    """Every call the pair's tries logged, oldest first, the chair's memo
    calls told apart by the memo turns the kept records name."""
    turns = [
        turn for t in run.tries if t.held.result is not None
        for turn in (_memo_turn(t.held.result.record),) if turn is not None
    ]
    logs = [read_call_log(path, memo_turns=turns) for path in sorted(directory.rglob(CALL_LOG))]
    return CallLog(
        tuple(sorted((r for log in logs for r in log.records), key=lambda r: r.started_at)),
        tuple(sorted((f for log in logs for f in log.failures), key=lambda f: f.started_at)),
    )


def _line(
    arm: str, series: Series, meeting: Meeting, attempt: int, meeting_try: int, held: Held[Any],
) -> dict[str, Any]:
    error = held.start_error
    return {
        "arm": arm,
        "series": series.id,
        "meeting": meeting.id,
        "attempt": attempt,
        "try": meeting_try,
        "start_failed": held.start_failed,
        "start_error": None if error is None else f"{type(error).__name__}: {error}",
        "errors": list(held.errors),
        "cut_short": held.cut_short,
        "record": None if held.result is None else plain(held.result),
    }


def _answer(meeting: Meeting, arm: str, record: Mapping[str, Any]) -> str | None:
    """What the meeting asked for, read from the arm's kept *record*: the
    memo, or the recall check's answers. None at a briefing, and when the
    answer is missing: a memo never written, or arm A's reply empty or cut
    off at its token limit (:attr:`ArmAReply.missing`)."""
    if meeting.kind is MeetingKind.BRIEFING:
        return None
    if arm == arm_a.ARM:
        reply = ArmAReply(**{
            **record, "stop_reason": StopReason(record["stop_reason"]),
            "asked_at": dt.datetime.fromisoformat(record["asked_at"]),
            "answered_at": dt.datetime.fromisoformat(record["answered_at"]),
        })
        return None if reply.missing else reply.text
    memo = record.get("memo")
    return None if memo is None else str(memo["content"])


def _outcome(held: Held[Any]) -> str:
    if held.start_failed:
        return "did not start"
    if held.cut_short:
        return f"cut short ({', '.join(held.errors)})"
    return "held" + (f"; failed calls: {', '.join(held.errors)}" if held.errors else "")


def _try(line: Mapping[str, Any], meeting: Meeting, arm: str) -> Try[Kept]:
    record = line["record"]
    result = None if record is None else Kept(_answer(meeting, arm, record), record)
    held = Held(result, errors=tuple(line["errors"]), start_failed=line["start_failed"])
    return Try(line["meeting"], line["attempt"], line["try"], held)


def _memo_turn(record: Mapping[str, Any]) -> MemoTurn | None:
    turn = record.get("memo_turn")
    if not turn:
        return None
    return MemoTurn(**{**turn, "asked_at": dt.datetime.fromisoformat(turn["asked_at"])})


def _lines(path: Path) -> Iterator[dict[str, Any]]:
    if not path.exists():
        return
    for text in path.read_text(encoding="utf-8").splitlines():
        yield json.loads(text)


def _set_aside(directory: Path, interrupted: Path, stem: str) -> Path:
    """Move a pair stopped partway into *interrupted*, under the first free number."""
    interrupted.mkdir(parents=True, exist_ok=True)
    for number in itertools.count(1):
        aside = interrupted / f"{stem}-{number}"
        if not aside.exists():
            directory.rename(aside)
            return aside
    raise AssertionError("unreachable")
