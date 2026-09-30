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
  arrives and never asked for again, whatever it says.
- **Reading the answer.** The JSON the prompt asks for, which may sit in a
  code fence or after a line of prose. An answer the harness cannot read, or
  that breaks the rubric's scale, stops judging: :class:`JudgeFault`, a
  harness fault. The answer stays kept, so a reviewed fix to the reader can
  read it again without a second pass.
- **The call log.** Every call is logged with its own purpose, ``judge``, to
  its batch's own file, tagged with the packet's ID and never its arm, so the
  log keeps the seal. :func:`read_judge_log` reads it back for the cap.
- **The cap.** Before each call the batch's spend so far is priced; once it
  reaches $25, judging stops and the result is inconclusive (part 2 §4).

A batch keeps its answers in its directory as they arrive, so one stopped by
a fault, a provider outage or a crash resumes where it stopped.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from agents import call_log
from agents.llm_client import LLMClient
from agents.llm_types import LLMCallPurpose, StopReason
from evaluators.exp001.attempts import RETRIES, RETRY_WAITS, ErrorKind, error_kind
from evaluators.exp001.costs import CallPurpose, CallRecord, judging_spend
from evaluators.exp001.materials import MeetingKind, PlanKey, RecallItem
from evaluators.exp001.packets import (
    MemoPacket,
    RecallPacket,
    memo_packet_text,
    recall_packet_text,
)
from evaluators.exp001.runtime import CallLog, CallLogError, FailedCall
from evaluators.exp001.scoring import CRITERIA, MEMORY_CRITERION

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

_MEMO, _RECALL = "memo", "recall"
_SCALE = (0, 1, 2)
_MARKS = {"right": True, "wrong": False}
_TAGS = ("rater", "batch", "packet", "kind", "try")


class JudgeFault(RuntimeError):  # noqa: N818 — pre-registration §3 vocabulary
    """Judging stopped on something the harness got wrong: a request the
    provider refused, or an answer it cannot read. The fix goes through a
    reviewed PR, as for any harness fault."""


class UnreadableReplyError(ValueError):
    """The judge's answer is not the JSON its prompt asks for, or breaks the scale."""


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


# ─── Reading an answer ───────────────────────────────────────


@dataclass(frozen=True)
class MemoScores:
    """The judge's scores for one memo, as :func:`scoring.rater_total` reads them."""

    scores: dict[str, int | None]
    problems_found: tuple[str, ...]
    facts_used: tuple[str, ...]
    reasons: dict[str, str]


def read_memo_reply(text: str, key: PlanKey) -> MemoScores:
    """The scores in the judge's answer; C2 is null exactly when the key lists no facts."""
    reply = _json_object(text)
    scores: dict[str, int | None] = {}
    for criterion in CRITERIA:
        if criterion not in reply:
            raise UnreadableReplyError(f"no score for {criterion}")
        score = reply[criterion]
        if criterion == MEMORY_CRITERION and not key.earlier_facts:
            if score is not None:
                raise UnreadableReplyError(f"{criterion} scored, but the key lists no facts")
        # type() rather than isinstance(): True and False are ints in Python.
        elif type(score) is not int or score not in _SCALE:
            raise UnreadableReplyError(f"{criterion} is {score!r}, not 0, 1 or 2")
        scores[criterion] = score
    return MemoScores(
        scores=scores,
        problems_found=_names(reply, "C1_problems_found"),
        facts_used=_names(reply, "C2_facts_used"),
        reasons=_reasons(reply),
    )


def read_recall_reply(text: str, key: Mapping[str, RecallItem]) -> dict[str, bool]:
    """Each answer right or wrong, for exactly the key's questions."""
    reply = _json_object(text)
    extra = sorted(set(reply) - set(key))
    if extra:
        raise UnreadableReplyError(f"marks for questions the key does not have: {', '.join(extra)}")
    marks = {}
    for rid in key:
        mark = reply.get(rid)
        if not isinstance(mark, str) or mark.strip().lower() not in _MARKS:
            raise UnreadableReplyError(f"{rid} is {mark!r}, not right or wrong")
        marks[rid] = _MARKS[mark.strip().lower()]
    return marks


def _json_object(text: str) -> dict[str, Any]:
    """The JSON object from the first ``{`` to the last ``}``, so a code fence
    or a line of prose around it does no harm."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise UnreadableReplyError("no JSON object in the answer")
    try:
        reply = json.loads(text[start : end + 1])
    except ValueError as exc:
        raise UnreadableReplyError(f"not JSON: {exc}") from None
    if not isinstance(reply, dict):
        raise UnreadableReplyError("no JSON object in the answer")
    return reply


def _names(reply: Mapping[str, Any], field: str) -> tuple[str, ...]:
    names = reply.get(field, [])
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise UnreadableReplyError(f"{field} is not a list of IDs")
    return tuple(names)


def _reasons(reply: Mapping[str, Any]) -> dict[str, str]:
    reasons = reply.get("reasons", {})
    if not isinstance(reasons, dict) or not all(isinstance(v, str) for v in reasons.values()):
        raise UnreadableReplyError("reasons is not a map of criteria to sentences")
    return {str(k): v for k, v in reasons.items()}


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
    """
    directory.mkdir(parents=True, exist_ok=True)
    log, kept = directory / CALL_LOG, directory / REPLIES
    answers = _read_answers(kept)
    stray = sorted(set(answers) - {p.id for p in packets})
    if stray:
        raise JudgeFault(f"{kept}: answers for packets not in this batch: {', '.join(stray)}")
    memo_scores: dict[str, MemoScores] = {}
    recall_marks: dict[str, dict[str, bool]] = {}
    left: list[str] = []
    for packet in packets:
        answer = answers.get(packet.id)
        if answer is None:
            if left or judging_spend(read_judge_log(log).records) >= cap:
                left.append(packet.id)
                continue
            answer = await _ask(client, packet, prompts, log, batch, retries, sleep)
            _keep(kept, answer)
        try:
            if isinstance(packet, MemoPacket):
                memo_scores[packet.id] = read_memo_reply(_text(answer), packet.key)
            else:
                recall_marks[packet.id] = read_recall_reply(_text(answer), packet.key)
        except UnreadableReplyError as exc:
            unreadable = f"packet {packet.id}: the judge's answer is unreadable"
            raise JudgeFault(f"{unreadable}: {exc}") from exc
    spend = judging_spend(read_judge_log(log).records)
    return Judged(memo_scores, recall_marks, spend, cap_reached=bool(left), left=tuple(left))


def packet_kind(packet: Packet) -> MeetingKind:
    """The kind of meeting a packet answers: a control plan's key lists no facts."""
    if isinstance(packet, RecallPacket):
        return MeetingKind.RECALL
    return MeetingKind.PLAN if packet.key.earlier_facts else MeetingKind.CONTROL


async def _ask(
    client: LLMClient, packet: Packet, prompts: JudgePrompts, log: Path, batch: str,
    retries: int, sleep: Callable[[float], Awaitable[None]],
) -> dict[str, str]:
    """One pass at *packet*: the answer as it arrived, tried again after a provider error."""
    if isinstance(packet, MemoPacket):
        system, content, kind = prompts.memo, memo_packet_text(packet), _MEMO
    else:
        system, content, kind = prompts.recall, recall_packet_text(packet), _RECALL
    judge_try = 0
    while True:
        judge_try += 1
        tags = {
            "rater": RATER, "batch": batch, "packet": packet.id,
            "kind": packet_kind(packet).value, "try": str(judge_try),
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
            if judge_try > retries:
                raise
            await sleep(RETRY_WAITS[min(judge_try, len(RETRY_WAITS)) - 1])
            continue
        return {
            "packet": packet.id,
            "kind": kind,
            "text": response.text or "",
            "stop_reason": response.stop_reason.value,
        }


def _text(answer: Mapping[str, str]) -> str:
    """An answer's text, if it finished; one cut off at the token limit is unreadable."""
    if answer["stop_reason"] == StopReason.MAX_TOKENS.value:
        raise UnreadableReplyError("the answer was cut off at the token limit")
    return answer["text"]


def _keep(path: Path, answer: Mapping[str, str]) -> None:
    """Append one answer as a line, in one write, flushed before it is read."""
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, (json.dumps(answer) + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_answers(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    answers = {}
    for number, text in enumerate(path.read_text().splitlines(), start=1):
        try:
            answer = json.loads(text)
            answers[str(answer["packet"])] = {k: str(answer[k]) for k in (
                "packet", "kind", "text", "stop_reason",
            )}
        except (ValueError, KeyError, TypeError) as exc:
            raise JudgeFault(f"{path.name}:{number}: not a kept answer ({exc!r})") from exc
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
