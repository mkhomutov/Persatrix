"""What the EXP-001 harness tells each agent process, and what it reads back.

Before a meeting the harness starts every adviser with two groups of
settings. The **meeting clock** puts the adviser on the meeting's story date:
its agent time starts at 10:00 UTC that day and runs on with the real clock
(check 7). The **call log** names the file the adviser appends a line to for
every model call, and the tags that say which arm, series, meeting and
attempt the process is serving (check 4). Calls the harness makes itself,
such as arm A's, are tagged per meeting with :func:`call_log_scope`.

After the run, :func:`read_call_log` turns those lines into the
:class:`~evaluators.exp001.costs.CallRecord` values ``costs`` prices. It
maps the runtime's purposes onto the pre-registered ones. A reflexion critic
or rewrite is part of the adviser's reply; working-memory compression is a
memory summary. The chair's calls after the harness asks for the memo are
the memo. A memory write in an arm with no memory is kept but does not
count. A line the harness cannot place is refused, never guessed at.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agents import call_log
from agents.call_log import CALL_LOG_ENV, CALL_TAGS_ENV
from agents.clock import CLOCK_ANCHOR_ENV, CLOCK_START_ENV
from evaluators.exp001.costs import ARMS, CallPurpose, CallRecord
from evaluators.exp001.materials import MeetingKind

STORY_CLOCK_START = dt.time(10, 0, tzinfo=dt.UTC)

# Arms whose design has no memory: their memory writes are recorded, and
# counted in real spend, but not in the arm's dollars per plan.
MEMORYLESS_ARMS = frozenset({"B", "C", "D-prime"})

_PURPOSES = {
    "turn": CallPurpose.REPLY,
    "critic": CallPurpose.REPLY,
    "revise": CallPurpose.REPLY,
    "bid": CallPurpose.BID,
    "summary": CallPurpose.SUMMARY,
    "compress": CallPurpose.SUMMARY,
}
# Purposes that are the chair speaking, so they become the memo once asked.
_SPEAKING = frozenset({"turn", "critic", "revise"})
_TAGS = ("arm", "series", "meeting", "meeting_kind", "attempt")


class CallLogError(ValueError):
    """A call-log line the harness cannot turn into a record."""


def meeting_clock_env(story_date: dt.date, began_at: dt.datetime) -> dict[str, str]:
    """The clock settings for an adviser serving the meeting on *story_date*.

    *began_at* is the real moment the meeting began. Every start or restart
    within the meeting passes the same one, so the clock resumes rather than
    starting again from 10:00.
    """
    if began_at.tzinfo is None:
        raise ValueError("the meeting's real start needs a zone")
    start = dt.datetime.combine(story_date, STORY_CLOCK_START)
    return {CLOCK_START_ENV: start.isoformat(), CLOCK_ANCHOR_ENV: began_at.isoformat()}


def call_log_env(
    path: Path,
    *,
    arm: str,
    series: str,
    meeting: str,
    meeting_kind: MeetingKind,
    attempt: int,
) -> dict[str, str]:
    """The call-log settings for a process serving one meeting of one arm."""
    tags = _tags(arm, series, meeting, meeting_kind, attempt)
    return {CALL_LOG_ENV: str(path), CALL_TAGS_ENV: json.dumps(tags)}


def call_log_scope(
    path: Path,
    *,
    arm: str,
    series: str,
    meeting: str,
    meeting_kind: MeetingKind,
    attempt: int,
) -> AbstractContextManager[None]:
    """The same log and tags for the calls the harness makes inside the block."""
    return call_log.scoped(path, _tags(arm, series, meeting, meeting_kind, attempt))


def _tags(
    arm: str, series: str, meeting: str, meeting_kind: MeetingKind, attempt: int,
) -> dict[str, str]:
    if arm not in ARMS:
        raise ValueError(f"unknown arm {arm!r}; expected one of {ARMS}")
    return {
        "arm": arm,
        "series": series,
        "meeting": meeting,
        "meeting_kind": meeting_kind.value,
        "attempt": str(attempt),
    }


@dataclass(frozen=True)
class MemoTurn:
    """When the harness asked a meeting's chair for the memo."""

    arm: str
    series: str
    meeting: str
    attempt: int
    chair: str
    asked_at: dt.datetime

    def __post_init__(self) -> None:
        # Call-log times carry a zone; a bare one could not be compared.
        if self.asked_at.tzinfo is None:
            raise ValueError("the memo request's time needs a zone")


@dataclass(frozen=True)
class FailedCall:
    """A call that raised instead of answering; it has no token counts."""

    arm: str
    series: str
    meeting: str
    attempt: int
    adviser: str | None
    started_at: dt.datetime
    error: str


@dataclass(frozen=True)
class CallLog:
    records: tuple[CallRecord, ...]
    failures: tuple[FailedCall, ...]


def read_call_log(path: Path, *, memo_turns: Iterable[MemoTurn] = ()) -> CallLog:
    """Every line of *path*, as records and failures. No file is an empty log."""
    memos = {(m.arm, m.series, m.meeting, m.attempt): m for m in memo_turns}
    records: list[CallRecord] = []
    failures: list[FailedCall] = []
    if not path.exists():
        return CallLog((), ())
    for number, text in enumerate(path.read_text().splitlines(), start=1):
        where = f"{path.name}:{number}"
        try:
            line = json.loads(text)
        except ValueError as exc:
            raise CallLogError(f"{where}: not JSON") from exc
        record = _record(line, where, memos)
        if line.get("error") is not None:
            failures.append(FailedCall(
                arm=record.arm, series=record.series, meeting=record.meeting,
                attempt=record.attempt, adviser=record.adviser,
                started_at=record.started_at, error=str(line["error"]),
            ))
        else:
            records.append(record)
    return CallLog(tuple(records), tuple(failures))


def _record(
    line: Any, where: str, memos: dict[tuple[str, str, str, int], MemoTurn],
) -> CallRecord:
    if not isinstance(line, dict):
        raise CallLogError(f"{where}: not a JSON object")
    tags = line.get("tags") or {}
    missing = [t for t in _TAGS if t not in tags]
    if missing:
        raise CallLogError(f"{where}: tags lack {', '.join(missing)}")
    runtime_purpose = line.get("purpose")
    if runtime_purpose not in _PURPOSES:
        raise CallLogError(f"{where}: unknown purpose {runtime_purpose!r}")
    try:
        arm = tags["arm"]
        attempt = int(tags["attempt"])
        kind = MeetingKind(tags["meeting_kind"])
        started_at = dt.datetime.fromisoformat(line["started_at"])
        agent_id = line["agent_id"]
        model = line["model"]
        tokens = {
            key: int(line[key])
            for key in ("input_tokens", "output_tokens", "cache_write_tokens", "cache_read_tokens")
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise CallLogError(f"{where}: {exc!r}") from exc
    if arm not in ARMS:
        raise CallLogError(f"{where}: unknown arm {arm!r}")

    purpose = _PURPOSES[runtime_purpose]
    memo = memos.get((arm, tags["series"], tags["meeting"], attempt))
    if (
        memo is not None
        and runtime_purpose in _SPEAKING
        and agent_id == memo.chair
        and started_at >= memo.asked_at
    ):
        purpose = CallPurpose.MEMO
    return CallRecord(
        arm=arm,
        series=tags["series"],
        meeting=tags["meeting"],
        meeting_kind=kind,
        attempt=attempt,
        # Arm A is one call that plays every adviser, so it has none of its own.
        adviser=None if arm == "A" else agent_id,
        purpose=purpose,
        started_at=started_at,
        model=model,
        counts_in_arm=not (purpose is CallPurpose.SUMMARY and arm in MEMORYLESS_ARMS),
        **tokens,
    )
