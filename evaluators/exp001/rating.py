"""The packets each rater scores, drawn once and kept.

Once every pair of a run has been held, the harness gathers each answer the
meetings asked for (:func:`gather_answers`): every memo that was written,
and every recall check's answers. It blinds them into packets and draws
each rater's order, the two people's and the judge's
(:mod:`evaluators.exp001.packets`). It does so once (:func:`draw_packets`)
and keeps what it drew, so a run started again finds the same packet IDs in
the same orders, and asks the judge exactly what it asked before:

- ``packets.json`` holds every packet, and each rater's order by packet ID;
  it is written last, so without it nothing was drawn;
- ``raters/<person>.txt`` gives each person their packets in their own
  order, each headed by its ID and shown as every rater reads it;
- ``sealed/seal.json`` holds the seal, from packet ID back to arm, series
  and meeting, kept apart until every score is in. With it go the answers
  that were missing, since a missing memo scores 0 and never reaches a
  rater, and each memo's length and whether it was cut at 400 words.

A dropped series is dropped from every arm's comparisons, so none of its
answers is gathered.
"""

from __future__ import annotations

import json
import random
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

from evaluators.exp001.attempts import SeriesRun
from evaluators.exp001.deployed_meeting import plain
from evaluators.exp001.judge import RATER, Packet
from evaluators.exp001.materials import FactUse, MeetingKind, PlanKey, RecallItem, Series
from evaluators.exp001.packets import (
    MemoPacket,
    RecallPacket,
    blind,
    memo_packet_text,
    random_id,
    rater_orders,
    recall_packet_text,
)
from evaluators.exp001.pairs import Kept, write_json
from evaluators.exp001.scoring import MemoRef

PEOPLE = ("person-1", "person-2")
RATERS = (*PEOPLE, RATER)
PACKETS = "packets.json"
RATER_FILES = "raters"
SEALED = "sealed"
SEAL = "seal.json"


@dataclass(frozen=True)
class Answers:
    """Every answer a run's meetings asked for, by arm, series and meeting."""

    memos: dict[MemoRef, str]
    recall_replies: dict[MemoRef, str]
    missing: tuple[MemoRef, ...]  # never written, so scored 0 without a rater


@dataclass(frozen=True)
class Drawn:
    """The packets, by ID, and each rater's order."""

    packets: dict[str, Packet]
    orders: dict[str, list[str]]

    def order(self, rater: str) -> list[Packet]:
        return [self.packets[pid] for pid in self.orders[rater]]


class Cut(NamedTuple):
    words: int  # the memo's words as written
    cut: bool  # whether the harness cut it at 400


@dataclass(frozen=True)
class Seal:
    """The map from packet ID back to arm, and what the raters never see."""

    packets: dict[str, MemoRef]
    missing: tuple[MemoRef, ...]
    cuts: dict[MemoRef, Cut]


def gather_answers(runs: Iterable[SeriesRun[Kept]], series: Mapping[str, Series]) -> Answers:
    """The answer each finished try holds, for every plan meeting and recall
    check; one that is missing is listed instead."""
    memos: dict[MemoRef, str] = {}
    recall_replies: dict[MemoRef, str] = {}
    missing: list[MemoRef] = []
    for run in runs:
        finished = run.finished()  # empty when the series was dropped
        for meeting in series[run.series].meetings:
            if meeting.kind is MeetingKind.BRIEFING or run.dropped:
                continue
            ref = MemoRef(run.arm, run.series, meeting.id)
            kept = finished[meeting.id].held.result
            answer = None if kept is None else kept.answer
            if answer is None:
                missing.append(ref)
            elif meeting.kind is MeetingKind.RECALL:
                recall_replies[ref] = answer
            else:
                memos[ref] = answer
    return Answers(memos, recall_replies, tuple(missing))


def draw_packets(
    directory: Path,
    series: Sequence[Series],
    answers: Answers,
    names: Sequence[str],
    *,
    new_id: Callable[[], str] = random_id,
    rng: random.Random | None = None,
) -> Drawn:
    """Blind *answers* into packets and draw each rater's order, once.

    What was drawn is kept in *directory*; drawn before, it is read back as
    it was, and answers that differ from the ones it was drawn from are
    refused. *names* are the advisers' IDs and names, which no rater reads.
    """
    if (directory / PACKETS).exists():
        seal = read_seal(directory)
        drawn_from = (set(seal.packets.values()), set(seal.missing))
        if drawn_from != ({*answers.memos, *answers.recall_replies}, set(answers.missing)):
            raise ValueError(f"{directory}: the answers changed since the packets were drawn")
        return read_packets(directory)
    blinded = blind(series, answers.memos, answers.recall_replies, names, new_id=new_id)
    every: list[Packet] = [*blinded.memo_packets, *blinded.recall_packets]
    packets = {packet.id: packet for packet in every}
    orders = rater_orders(list(packets), RATERS, rng=rng)
    (directory / SEALED).mkdir(parents=True, exist_ok=True)
    write_json(directory / SEALED / SEAL, {
        "packets": {pid: ref._asdict() for pid, ref in blinded.seal.items()},
        "missing": [ref._asdict() for ref in answers.missing],
        "cuts": [{**ref._asdict(), "words": cut.words, "cut": cut.cut}
                 for ref, cut in blinded.cuts.items()],
    })
    (directory / RATER_FILES).mkdir(exist_ok=True)
    for person in PEOPLE:
        shown = _shown([packets[pid] for pid in orders[person]])
        (directory / RATER_FILES / f"{person}.txt").write_text(shown)
    write_json(directory / PACKETS, {
        "packets": [{"kind": _kind(p), **plain(p)} for p in packets.values()],
        "orders": orders,
    })
    return Drawn(packets, orders)


def read_packets(directory: Path) -> Drawn:
    """The packets and orders drawn in *directory*."""
    kept = json.loads((directory / PACKETS).read_text())
    packets = {raw["id"]: _packet(raw) for raw in kept["packets"]}
    return Drawn(packets, {rater: list(order) for rater, order in kept["orders"].items()})


def read_seal(directory: Path) -> Seal:
    """The seal kept beside the packets drawn in *directory*."""
    sealed = json.loads((directory / SEALED / SEAL).read_text())
    return Seal(
        packets={pid: MemoRef(**ref) for pid, ref in sealed["packets"].items()},
        missing=tuple(MemoRef(**ref) for ref in sealed["missing"]),
        cuts={
            MemoRef(c["arm"], c["series"], c["meeting"]): Cut(c["words"], c["cut"])
            for c in sealed["cuts"]
        },
    )


def packet_text(packet: Packet) -> str:
    """The packet as every rater reads it."""
    if isinstance(packet, MemoPacket):
        return memo_packet_text(packet)
    return recall_packet_text(packet)


def _shown(packets: Sequence[Packet]) -> str:
    """A person's packets in their order, each headed by its ID."""
    return "\n".join(
        f"=== Packet {packet.id}, {number} of {len(packets)} ===\n\n{packet_text(packet)}"
        for number, packet in enumerate(packets, start=1)
    )


def _kind(packet: Packet) -> str:
    return "memo" if isinstance(packet, MemoPacket) else "recall"


def _packet(raw: Mapping[str, Any]) -> Packet:
    if raw["kind"] == "memo":
        key = raw["key"]
        return MemoPacket(
            id=raw["id"],
            organisation=raw["organisation"],
            messages=tuple(raw["messages"]),
            key=PlanKey(
                problems=dict(key["problems"]),
                earlier_facts={
                    fid: FactUse(use["implication"], tuple(use["must_state"]))
                    for fid, use in key["earlier_facts"].items()
                },
                unsound_options=tuple(key["unsound_options"]),
            ),
            memo=raw["memo"],
        )
    return RecallPacket(
        id=raw["id"],
        organisation=raw["organisation"],
        key={
            rid: RecallItem(item["fact"], item["answer"], tuple(item["must_include"]))
            for rid, item in raw["key"].items()
        },
        reply=raw["reply"],
    )
