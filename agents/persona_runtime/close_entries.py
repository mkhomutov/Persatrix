"""What a closed record is DERIVED FROM — and whose words those are.

Split out of :mod:`agents.persona_runtime.summarize_close` (ISSUE-0131
review): that module sat at exactly the 500-line cap enforced by
``scripts/checks/file_size.py --strict``, so the §G guard the review
asked for could not be added with its rationale — the third such split
in this PR, after ``interaction_key`` and ``_facts_write``, and the same
reason each time.  ``summarize_close`` keeps the LLM round trip and the
envelope parsing; this module owns the step before it: turning a
record's turns into the derivation input.

The seam is where the ISSUE-0131 correctness argument lives, so it is
worth naming.  The ``speaker_id`` projection stamps one speaker onto the
episode row and onto every fact a close extracts, which is sound ONLY
because the ``(principal, speaker, scope)`` record key makes each record
single-speaker by construction.  That premise has exactly one breach —
the RFC 0020 §G room-close turn, which the close-notification fan lands
as the final turn of EVERY sibling record it closes — and
:func:`is_foreign_room_close_turn` is where it is discharged, as EXCLUDE
rather than tag: the turn never reaches the combined summarise+extract
call, so no summary and no fact can be derived from one speaker's words
and then attributed to another's.

:func:`own_turn_items` is the chokepoint (PR #849 review round 3): every
close-pipeline consumer of ``interaction.turns`` reads the record
through it — :func:`interaction_to_entries` (the ordinary multi-turn
path), the ``turn_count == 1`` fast path in ``summarize_close``, and
``close_path.persist_closed_interaction`` for the turns it persists
into ``context_json`` (the FTS-indexed column recall searches, where
the closer's sender must not surface on a row stamped with another
speaker) — so a new consumer that reaches for the filtered view gets
the exclusion by construction instead of by remembering a predicate.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..memory.interaction_types import ROOM_CLOSE_TURN_KEY
from ..memory.store import MemoryEntry

if TYPE_CHECKING:
    from ..memory.interactions import Interaction, Turn

__all__ = [
    "interaction_to_entries",
    "is_foreign_room_close_turn",
    "own_turn_items",
]


def is_foreign_room_close_turn(
    payload: dict[str, object], speaker_id: str,
) -> bool:
    """The RFC 0020 §G exception: a room-close turn someone ELSE spoke.

    The close-notification fan lands one closing message as the final
    turn of EVERY record it closes, so on all but one that turn's sender
    is not the record's speaker.  ISSUE-0131 projects
    ``interaction.speaker_id`` onto the episode row and every extracted
    fact, which is sound only because a record is single-speaker by
    construction — this turn is the sole breach, so it is dropped from
    the derivation input (the RFC's "exclude or tag", discharged as
    exclude).  Without it a fact from the closer's words is attributed to
    the record's own speaker: the Phase 0b defect itself.

    Keyed off the producer's recorded stamp, not a guess.  The sender
    comparison decides only WHOSE record this is — on the closer's own
    record the turn is native, so it stays.

    Deliberately NOT the same rule as the read-side skip in
    ``closed_interactions_read._participants``, which drops EVERY
    stamped turn: participants exclude the close event regardless of
    nativity, and that spelling must also cover pre-#849 rows whose
    ``context_json`` still holds the foreign turn this predicate now
    keeps out at write time.  Change one without re-reading the other.
    """
    if payload.get(ROOM_CLOSE_TURN_KEY) is not True:
        return False
    return str(payload.get("sender", "")).strip() != speaker_id


def own_turn_items(interaction: Interaction) -> list[tuple[int, Turn]]:
    """The record's own turns, with their REAL ordinals — the §G chokepoint.

    One filtered view for every close-pipeline consumer of
    ``interaction.turns`` (the module header names the three), so the
    exclusion is applied by construction rather than by each site
    remembering :func:`is_foreign_room_close_turn`.  Ordinals are the
    record's real turn positions: a dropped turn does not renumber its
    siblings or shift the compressor's importance weights.
    """
    return [
        (idx, turn)
        for idx, turn in enumerate(interaction.turns, start=1)
        if not is_foreign_room_close_turn(
            turn.payload or {}, interaction.speaker_id)
    ]


def interaction_to_entries(interaction: Interaction) -> list[MemoryEntry]:
    """Project per-turn payloads into ``MemoryEntry`` shape for compress().

    Each turn becomes one entry; importance equals the turn ordinal
    normalised into ``(0, 1]`` so later turns weigh slightly more
    than openers when the compressor has to drop entries.

    A foreign room-close turn is SKIPPED (:func:`own_turn_items`).
    Ordinals still come from the record's real turn positions, so a drop
    does not renumber its siblings or shift their weights.
    """
    total = max(interaction.turn_count, 1)
    entries: list[MemoryEntry] = []
    for idx, turn in own_turn_items(interaction):
        payload = turn.payload or {}
        content_parts: list[str] = []
        # ISSUE-0054 — ``text`` (inbound message body) is the load-bearing
        # input for RFC 0026 extraction; ``summary`` is the action envelope.
        for key in ("text", "summary"):
            value = str(payload.get(key, "")).strip()
            if value:
                content_parts.append(value)
        sender = str(payload.get("sender", "")).strip()
        if sender:
            content_parts.append(f"sender={sender}")
        if not content_parts:
            content_parts.append(
                f"event_type={payload.get('event_type', 'unknown')}",
            )
        entries.append(MemoryEntry(
            id=f"turn-{idx}",
            content=" | ".join(content_parts),
            importance=idx / total,
            tags=(),
            created_at=turn.at,
            score=0.0,
        ))
    return entries
