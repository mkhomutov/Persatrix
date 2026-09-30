"""The blinded packets EXP-001's raters score.

Part 2 §2 of the pre-registration: a rater's packet for a memo holds the
organisation's one-line description, every message the operator sent in that
series up to and including the plan the memo answers, the plan's answer key,
and the memo, cut at 400 words. Nothing in it says which arm wrote it:

- every adviser's name is replaced with a placeholder;
- every packet gets a random ID from the operating system's random source,
  so nobody can rebuild the IDs from a seed;
- each rater gets the packets in a different shuffled order;
- the **seal**, the map from ID back to arm, series and meeting, is kept
  apart from the packets until every score is in.

Recall answers get packets of their own: the answer key and the reply.

Every rater reads a packet as the same text, :func:`memo_packet_text` or
:func:`recall_packet_text`, the LLM judge included: the key shows each of
its IDs, which the judge's reply names.
"""

from __future__ import annotations

import math
import random
import re
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

import yaml

from evaluators.exp001.materials import Meeting, MeetingKind, PlanKey, RecallItem, Series
from evaluators.exp001.scoring import CutMemo, MemoRef, cut_memo

ADVISER_PLACEHOLDER = "[adviser]"

T = TypeVar("T")


@dataclass(frozen=True)
class MemoPacket:
    id: str
    organisation: str
    messages: tuple[str, ...]  # the briefing, earlier plans and this plan, in order
    key: PlanKey
    memo: str


@dataclass(frozen=True)
class RecallPacket:
    id: str
    organisation: str
    key: dict[str, RecallItem]
    reply: str


@dataclass(frozen=True)
class Blinded:
    memo_packets: tuple[MemoPacket, ...]
    recall_packets: tuple[RecallPacket, ...]
    seal: dict[str, MemoRef]  # kept from the raters until every score is in
    cuts: dict[MemoRef, CutMemo]  # where each memo was cut, for the report


def load_adviser_names(panel: Path) -> tuple[str, ...]:
    """Every adviser's ID and display name, from panel.yaml."""
    doc = yaml.safe_load(panel.read_text())
    return tuple(str(a[field]) for a in doc["advisers"] for field in ("id", "name"))


def redact(text: str, names: Sequence[str]) -> str:
    """Replace each name however it is cased or joined, then each word of it alone.

    A whole name matches in any case, joined by a space, hyphen, underscore or
    line break. A single word of a name ("Stoat") matches only capitalised, so
    an ordinary word such as "lunar" is left alone.
    """
    words: set[str] = set()
    for name in names:
        parts = [p for p in re.split(r"[\s_-]+", name.strip()) if p]
        words.update(p.capitalize() for p in parts)
        joined = r"[\s_-]*".join(re.escape(p) for p in parts)
        text = re.sub(_bounded(joined), ADVISER_PLACEHOLDER, text, flags=re.IGNORECASE)
    for word in words:
        text = re.sub(_bounded(re.escape(word)), ADVISER_PLACEHOLDER, text)
    return text


def _bounded(pattern: str) -> str:
    # Not \b, which counts "_" as part of a word and so misses "lunar_stoat".
    return r"(?<![^\W_])" + pattern + r"(?![^\W_])"


def blind(
    series: Sequence[Series],
    memos: Mapping[MemoRef, str],
    recall_replies: Mapping[MemoRef, str],
    names: Sequence[str],
    *,
    new_id: Callable[[], str] = lambda: secrets.token_hex(4),
) -> Blinded:
    """Build every packet and the seal.

    ``memos`` holds only memos that were written; a missing memo scores 0
    without going to the raters.
    """
    by_id = {s.id: s for s in series}
    seal: dict[str, MemoRef] = {}

    def fresh(ref: MemoRef) -> str:
        pid = new_id()
        while pid in seal:
            pid = new_id()
        seal[pid] = ref
        return pid

    memo_packets, cuts = [], {}
    for ref, text in memos.items():
        s, meeting, earlier = _locate(by_id, ref)
        if not meeting.kind.scored or meeting.plan_key is None:
            raise ValueError(f"{ref.meeting}: a memo answers a plan, not a {meeting.kind.value}")
        cuts[ref] = cut_memo(text)
        memo_packets.append(
            MemoPacket(
                id=fresh(ref),
                organisation=s.organisation,
                messages=tuple(m.message for m in earlier),
                key=meeting.plan_key,
                memo=redact(cuts[ref].text, names),
            )
        )

    recall_packets = []
    for ref, reply in recall_replies.items():
        s, meeting, _ = _locate(by_id, ref)
        if meeting.kind is not MeetingKind.RECALL or meeting.recall_key is None:
            raise ValueError(f"{ref.meeting}: a recall reply answers the recall check")
        recall_packets.append(
            RecallPacket(
                id=fresh(ref),
                organisation=s.organisation,
                key=meeting.recall_key,
                reply=redact(reply, names),
            )
        )

    return Blinded(tuple(memo_packets), tuple(recall_packets), seal, cuts)


def _locate(by_id: Mapping[str, Series], ref: MemoRef) -> tuple[Series, Meeting, list[Meeting]]:
    """The series, the meeting, and every meeting up to and including it."""
    s = by_id[ref.series]
    ids = [m.id for m in s.meetings]
    if ref.meeting not in ids:
        raise ValueError(f"{ref.series} has no meeting {ref.meeting}")
    upto = list(s.meetings[: ids.index(ref.meeting) + 1])
    return s, upto[-1], upto


def memo_packet_text(packet: MemoPacket) -> str:
    """The memo packet as a rater reads it: the organisation, the operator's
    messages oldest first, the answer key, then the memo."""
    count = len(packet.messages)
    parts = [f"Organisation: {packet.organisation}"]
    for number, message in enumerate(packet.messages, start=1):
        answered = ", the request the memo answers" if number == count else ""
        parts.append(f"--- Operator message {number} of {count}{answered} ---\n{message.rstrip()}")
    parts.append(f"--- Answer key ---\n{_plan_key_text(packet.key)}")
    parts.append(f"--- Memo ---\n{packet.memo.rstrip()}")
    return "\n\n".join(parts) + "\n"


def _plan_key_text(key: PlanKey) -> str:
    lines = ["Problems inside the plan:"]
    lines += [f"{kid}: {problem}" for kid, problem in key.problems.items()]
    if key.earlier_facts:
        lines.append("Earlier facts that bear on the decision:")
        for fid, use in key.earlier_facts.items():
            lines += [f"{fid}: {use.implication}", "Must state:"]
            lines += [f"- {detail}" for detail in use.must_state]
    else:
        lines.append("Earlier facts that bear on the decision: none.")
    unsound = ", ".join(key.unsound_options)
    lines.append(f"Options those facts make unsound: {unsound}" if unsound else
                 "Options those facts make unsound: none.")
    return "\n".join(lines)


def recall_packet_text(packet: RecallPacket) -> str:
    """The recall packet as a rater reads it: the organisation, the answer
    key, each answer tied to the question it answers, then the reply."""
    lines = []
    for rid, item in packet.key.items():
        number = re.fullmatch(r"R(\d+)", rid)
        question = f", question {number.group(1)}" if number else ""
        lines += [f"{rid}{question}: {item.answer}", "Must include:"]
        lines += [f"- {detail}" for detail in item.must_include]
    parts = [
        f"Organisation: {packet.organisation}",
        "--- Answer key ---\n" + "\n".join(lines),
        f"--- Reply ---\n{packet.reply.rstrip()}",
    ]
    return "\n\n".join(parts) + "\n"


def rater_orders(
    packets: Sequence[T], raters: Sequence[str], *, rng: random.Random | None = None
) -> dict[str, list[T]]:
    """Each rater's packets in a shuffled order no other rater shares."""
    rng = rng or random.SystemRandom()
    distinct = math.factorial(len(packets)) >= len(raters)  # else some must share
    orders: dict[str, list[T]] = {}
    for rater in raters:
        order = list(packets)
        rng.shuffle(order)
        while distinct and order in orders.values():
            order = list(packets)
            rng.shuffle(order)
        orders[rater] = order
    return orders
