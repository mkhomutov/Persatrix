"""The LLM judge: the third of EXP-001's raters.

Part 2 §2 of the pre-registration: two people and an LLM judge score every
memo and every recall answer, each working alone. The judge is
``claude-opus-5`` with its default settings, one pass per packet, and its
prompts are in ``rubric.yaml``. This module is the judge.

- **Its prompts.** The rubric's two prompts, with ``{criteria}`` and
  ``{rule}`` filled by exact replacement, as the rubric's templating note
  asks. The prompt is the system prompt; the packet, rendered as every
  rater reads it (:mod:`evaluators.exp001.packets`), is the one user message.
- **One pass.** Each packet gets one answer. A provider error is retried, as
  the arms' are, since no answer came; an answer that came is kept as it
  arrives and never asked for again, whatever it says. One run at a time
  holds a batch, and a kept answer records what was asked, so a batch that
  cannot keep this promise stops rather than ask twice or mix two calls.
- **Reading the answer** (:mod:`evaluators.exp001.judge_answers`). The one
  JSON object the prompt asks for, which may sit in a code fence or among
  lines of prose. An answer the harness cannot read, a refusal, or one that
  breaks the rubric's scale stops judging: :class:`JudgeFault`, a harness
  fault. The answer stays kept, so before any scored meeting a reviewed fix
  to the reader can read it again without a second pass; in the scored
  judging, a harness fault discards every scored output instead
  (pre-registration §3).
- **The call log.** Every call is logged with its own purpose, ``judge``, to
  its batch's own file, tagged with the packet's ID and never its arm, so the
  log keeps the seal. :func:`read_judge_log` reads it back for the cap, and a
  line for another batch or packet stops judging.
- **The cap.** Before each call the batch's spend so far is priced; once it
  reaches $25, judging stops and the result is inconclusive (part 2 §4).

A batch keeps its answers in its directory as they arrive, so one stopped by
a fault, a provider outage or a crash resumes where it stopped.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import fcntl
import hashlib
import json
import os
from collections.abc import Awaitable, Callable, Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from agents import call_log
from agents.llm_client import LLMClient
from agents.llm_types import LLMCallPurpose, StopReason
from evaluators.exp001.attempts import (
    RETRIES,
    RETRY_WAITS,
    ErrorKind,
    HarnessFault,
    error_kind,
)
from evaluators.exp001.costs import CallPurpose, CallRecord, judging_spend
from evaluators.exp001.judge_answers import (
    MemoScores,
    UnreadableReplyError,
    read_memo_reply,
    read_recall_reply,
)
from evaluators.exp001.materials import MeetingKind
from evaluators.exp001.packets import (
    MemoPacket,
    RecallPacket,
    memo_packet_text,
    recall_packet_text,
)
from evaluators.exp001.runtime import CallLog, CallLogError, FailedCall

JUDGE_MODEL = "claude-opus-5"
# The API needs a limit. The model's default adaptive thinking counts toward
# it, and 16 000 keeps a request that is not streamed inside the provider
# library's time limit.
MAX_TOKENS = 16_000
# The API's own default. The adapter sends this model no temperature at all.
TEMPERATURE = 1.0
JUDGING_CAP = 25.0
RATER = "judge"
CALL_LOG = "calls.jsonl"
REPLIES = "replies.jsonl"
LOCK = "lock"

_MEMO, _RECALL = "memo", "recall"
_TAGS = ("rater", "batch", "packet", "kind", "try")
_REFUSAL = "refusal"  # Anthropic's stop reason for a request its classifiers decline
_KEPT = ("packet", "kind", "text", "stop_reason", "asked")


class JudgeFault(HarnessFault):  # noqa: N818 — pre-registration §3 vocabulary
    """Judging stopped on something the harness got wrong: a request the
    provider refused, an answer it cannot read, or a batch whose call log or
    kept answers break one pass. A harness fault like any other, so the run
    counts it, and the fix goes through a reviewed PR."""


@dataclass(frozen=True)
class JudgePrompts:
    memo: str
    recall: str


def load_prompts(rubric: Path) -> JudgePrompts:
    """The judge's two prompts from *rubric*, filled in."""
    doc = yaml.safe_load(rubric.read_text())
    judge = doc["judge"]
    return JudgePrompts(
        memo=_fill(judge["memo_prompt"], "{criteria}", criteria_text(doc["memo"]["criteria"])),
        recall=_fill(judge["recall_prompt"], "{rule}", str(doc["recall"]["rule"])),
    )


def _fill(prompt: str, placeholder: str, value: str) -> str:
    if prompt.count(placeholder) != 1:
        raise ValueError(f"the judge's prompt must hold {placeholder} exactly once")
    return prompt.replace(placeholder, value)


def criteria_text(criteria: Mapping[str, Any]) -> str:
    """The rubric's memo criteria, one block each: name, what it reads, anchors, note."""
    blocks = []
    for cid, criterion in criteria.items():
        lines = [f"{cid}: {criterion['name']}"]
        if criterion.get("reads"):
            lines.append(f"Reads: {criterion['reads']}")
        lines += [f"{score}: {anchor}" for score, anchor in criterion["anchors"].items()]
        if criterion.get("note"):
            lines.append(f"Note: {criterion['note']}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


# ─── Judging a batch ─────────────────────────────────────────

Packet = MemoPacket | RecallPacket


@dataclass(frozen=True)
class Judged:
    """What a batch gave: the judge's scores by packet ID, and why it stopped."""

    memo_scores: dict[str, MemoScores]
    recall_marks: dict[str, dict[str, bool]]
    spend: float  # every judge call in the batch's log, priced
    cap_reached: bool
    left: tuple[str, ...]  # packets never judged, because the cap was reached


async def judge_batch(
    client: LLMClient,
    packets: Sequence[Packet],
    prompts: JudgePrompts,
    directory: Path,
    *,
    batch: str,
    cap: float = JUDGING_CAP,
    retries: int = RETRIES,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> Judged:
    """Judge *packets* in the order given, the judge's own shuffled order.

    *directory* holds the batch's call log and the answers so far; *batch*
    names it in every log line: ``practice``, or ``scored`` for the scored run.
    One run at a time holds the directory; a second is refused.
    """
    directory.mkdir(parents=True, exist_ok=True)
    with sole_run(directory, RuntimeError(f"{directory}: another run is judging this batch")):
        return await _judge_batch(client, packets, prompts, directory, batch, cap, retries, sleep)


async def _judge_batch(
    client: LLMClient, packets: Sequence[Packet], prompts: JudgePrompts, directory: Path,
    batch: str, cap: float, retries: int, sleep: Callable[[float], Awaitable[None]],
) -> Judged:
    log, kept = directory / CALL_LOG, directory / REPLIES
    ids = {p.id for p in packets}
    if len(ids) != len(packets):
        raise JudgeFault("a packet is listed twice in the batch, so it would be asked twice")
    answers = _read_answers(kept)
    stray = sorted(set(answers) - ids)
    if stray:
        raise JudgeFault(f"{kept}: answers for packets not in this batch: {', '.join(stray)}")
    # A call the log shows answered, with no answer kept, was paid for and lost.
    lost = sorted({r.meeting for r in _batch_log(log, batch, ids)[0].records} - set(answers))
    if lost:
        raise JudgeFault(
            f"{log}: answered with no answer kept, so asking again would be a second pass: "
            f"{', '.join(lost)}",
        )
    memo_scores: dict[str, MemoScores] = {}
    recall_marks: dict[str, dict[str, bool]] = {}
    left: list[str] = []
    for packet in packets:
        answer = answers.get(packet.id)
        if answer is None:
            calls, spend = _batch_log(log, batch, ids)
            if left or spend >= cap:
                left.append(packet.id)
                continue
            tried = _last_try(calls, packet.id)
            answer = await _ask(client, packet, prompts, log, batch, tried, retries, sleep)
            _keep(kept, answer)
        elif answer["asked"] != _asked(packet, prompts):
            raise JudgeFault(
                f"packet {packet.id}: its kept answer was asked with another prompt, packet or "
                "call; judge the batch again in a directory of its own",
            )
        try:
            if isinstance(packet, MemoPacket):
                memo_scores[packet.id] = read_memo_reply(_text(answer), packet.key)
            else:
                recall_marks[packet.id] = read_recall_reply(_text(answer), packet.key)
        except UnreadableReplyError as exc:
            unreadable = f"packet {packet.id}: the judge's answer is unreadable"
            raise JudgeFault(f"{unreadable}: {exc}") from exc
    spend = _batch_log(log, batch, ids)[1]
    return Judged(memo_scores, recall_marks, spend, cap_reached=bool(left), left=tuple(left))


@contextmanager
def sole_run(directory: Path, busy: Exception) -> Iterator[None]:
    """Hold *directory* for one run, or raise *busy* while another holds it,
    since a second run at once would ask, or hold, what the first does. The
    lock goes with the process, so a crash leaves nothing to clear."""
    fd = os.open(directory / LOCK, os.O_WRONLY | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise busy from None
        yield
    finally:
        os.close(fd)


def _batch_log(log: Path, batch: str, packets: Collection[str]) -> tuple[CallLog, float]:
    """The batch's call log and its spend so far. A line the harness cannot read
    or price, or one for another batch or packet, is a harness fault."""
    try:
        calls = read_judge_log(log)
        spend = judging_spend(calls.records)
    except (KeyError, ValueError) as exc:  # a CallLogError is a ValueError
        raise JudgeFault(f"{log}: the batch's call log cannot be read: {exc}") from exc
    foreign = sorted({
        f"{line.series}/{line.meeting}" for line in _lines(calls)
        if line.series != batch or line.meeting not in packets
    })
    if foreign:
        raise JudgeFault(f"{log}: calls for another batch or packet: {', '.join(foreign)}")
    return calls, spend


def _last_try(calls: CallLog, packet: str) -> int:
    """The packet's last try in the log, answered or failed; 0 before its first."""
    return max((line.meeting_try for line in _lines(calls) if line.meeting == packet), default=0)


def _lines(calls: CallLog) -> list[CallRecord | FailedCall]:
    return [*calls.records, *calls.failures]


def packet_kind(packet: Packet) -> MeetingKind:
    """The kind of meeting a packet answers: a control plan's key lists no facts."""
    if isinstance(packet, RecallPacket):
        return MeetingKind.RECALL
    return MeetingKind.PLAN if packet.key.earlier_facts else MeetingKind.CONTROL


async def _ask(
    client: LLMClient, packet: Packet, prompts: JudgePrompts, log: Path, batch: str,
    tried: int, retries: int, sleep: Callable[[float], Awaitable[None]],
) -> dict[str, str]:
    """One pass at *packet*: the answer as it arrived, tried again after a provider
    error. Its tries count on from *tried*, the packet's last try in the log."""
    system, content = _request(packet, prompts)
    kind = _MEMO if isinstance(packet, MemoPacket) else _RECALL
    failed = 0
    while True:
        tags = {
            "rater": RATER, "batch": batch, "packet": packet.id,
            "kind": packet_kind(packet).value, "try": str(tried + failed + 1),
        }
        try:
            with call_log.scoped(log, tags):
                response = await client.create_message(
                    model=JUDGE_MODEL,
                    system=system,
                    messages=[{"role": "user", "content": content}],
                    tools=[],
                    max_tokens=MAX_TOKENS,
                    temperature=TEMPERATURE,
                    purpose=LLMCallPurpose.JUDGE,
                )
        except Exception as exc:
            if error_kind(type(exc).__name__) is not ErrorKind.PROVIDER:
                raise JudgeFault(
                    f"packet {packet.id}: the call failed with {type(exc).__name__}: {exc}",
                ) from exc
            failed += 1
            if failed > retries:
                raise
            await sleep(RETRY_WAITS[min(failed, len(RETRY_WAITS)) - 1])
            continue
        return {
            "packet": packet.id,
            "kind": kind,
            "text": response.text or "",
            # The provider's own stop reason, so a refusal is kept as one.
            "stop_reason": response.provider_stop_reason or response.stop_reason.value,
            "asked": _asked(packet, prompts),
        }


def _request(packet: Packet, prompts: JudgePrompts) -> tuple[str, str]:
    """The system prompt and the one user message *packet* is asked with."""
    if isinstance(packet, MemoPacket):
        return prompts.memo, memo_packet_text(packet)
    return prompts.recall, recall_packet_text(packet)


def _asked(packet: Packet, prompts: JudgePrompts) -> str:
    """What *packet* is asked, as a SHA-256 of the call's settings, prompt and packet text."""
    call = [JUDGE_MODEL, MAX_TOKENS, TEMPERATURE, *_request(packet, prompts)]
    return hashlib.sha256(json.dumps(call).encode()).hexdigest()


def _text(answer: Mapping[str, str]) -> str:
    """An answer's text, if it finished; one cut off at the token limit, or a
    refusal, is unreadable."""
    if answer["stop_reason"] == StopReason.MAX_TOKENS.value:
        raise UnreadableReplyError("the answer was cut off at the token limit")
    if answer["stop_reason"] == _REFUSAL:
        raise UnreadableReplyError("the judge refused to answer")
    return answer["text"]


def _keep(path: Path, answer: Mapping[str, str]) -> None:
    """Append one answer as a line, written out in full and flushed before it is read."""
    data = (json.dumps(answer) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        while data:  # a write can stop short, on a nearly full disk for one
            data = data[os.write(fd, data):]
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_answers(path: Path) -> dict[str, dict[str, str]]:
    """The answers kept so far; a second one for a packet could only be a second pass."""
    if not path.exists():
        return {}
    answers: dict[str, dict[str, str]] = {}
    for number, text in enumerate(path.read_text().splitlines(), start=1):
        try:
            answer = json.loads(text)
            kept = {k: str(answer[k]) for k in _KEPT}
        except (ValueError, KeyError, TypeError) as exc:
            raise JudgeFault(f"{path.name}:{number}: not a kept answer ({exc!r})") from exc
        if kept["packet"] in answers:
            raise JudgeFault(f"{path.name}:{number}: a second answer for packet {kept['packet']}")
        answers[kept["packet"]] = kept
    return answers


# ─── The judge's call log ────────────────────────────────────


def read_judge_log(path: Path) -> CallLog:
    """Every line of a batch's call log, as records and failures.

    A judge call names no arm, so the log keeps the seal: its record's arm is
    empty, its series the batch, and its meeting the packet's ID. A line
    from any other caller is refused.
    """
    records: list[CallRecord] = []
    failures: list[FailedCall] = []
    if not path.exists():
        return CallLog((), ())
    for number, text in enumerate(path.read_text().splitlines(), start=1):
        where = f"{path.name}:{number}"
        try:
            line = json.loads(text)
            tags = line["tags"]
            missing = [t for t in _TAGS if t not in tags]
            if missing or tags["rater"] != RATER:
                raise CallLogError(f"{where}: not a judge's line (tags {tags!r})")
            if line["purpose"] != LLMCallPurpose.JUDGE.value:
                raise CallLogError(f"{where}: purpose {line['purpose']!r}, not judge")
            record = CallRecord(
                arm="",
                series=tags["batch"],
                meeting=tags["packet"],
                meeting_kind=MeetingKind(tags["kind"]),
                attempt=1,
                adviser=None,
                purpose=CallPurpose.JUDGE,
                started_at=dt.datetime.fromisoformat(line["started_at"]),
                model=line["model"],
                input_tokens=int(line["input_tokens"]),
                output_tokens=int(line["output_tokens"]),
                cache_write_tokens=int(line["cache_write_tokens"]),
                cache_read_tokens=int(line["cache_read_tokens"]),
                meeting_try=int(tags["try"]),
            )
        except CallLogError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise CallLogError(f"{where}: {exc!r}") from exc
        if line.get("error") is not None:
            failures.append(FailedCall(
                arm=record.arm, series=record.series, meeting=record.meeting,
                attempt=record.attempt, meeting_try=record.meeting_try, adviser=None,
                started_at=record.started_at, error=str(line["error"]),
                purpose=CallPurpose.JUDGE,
            ))
        else:
            records.append(record)
    return CallLog(tuple(records), tuple(failures))
