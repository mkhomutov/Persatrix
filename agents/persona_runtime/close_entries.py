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

Two callers, and both must apply it or the exclusion has a hole:
:func:`interaction_to_entries` (the ordinary multi-turn path) and the
``turn_count == 1`` fast path in ``summarize_close``, which reads
``turns[0]`` directly and therefore has to ask the same question itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..memory.interaction_types import ROOM_CLOSE_TURN_KEY
from ..memory.store import MemoryEntry

if TYPE_CHECKING:
    from ..memory.interactions import Interaction

__all__ = ["interaction_to_entries", "is_foreign_room_close_turn"]


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
    """
    if payload.get(ROOM_CLOSE_TURN_KEY) is not True:
        return False
    return str(payload.get("sender", "")).strip() != speaker_id


def interaction_to_entries(interaction: Interaction) -> list[MemoryEntry]:
    """Project per-turn payloads into ``MemoryEntry`` shape for compress().

    Each turn becomes one entry; importance equals the turn ordinal
    normalised into ``(0, 1]`` so later turns weigh slightly more
    than openers when the compressor has to drop entries.

    A foreign room-close turn is SKIPPED (:func:`is_foreign_room_close_turn`).
    Ordinals still come from the record's real turn positions, so a drop
    does not renumber its siblings or shift their weights.
    """
    total = max(interaction.turn_count, 1)
    entries: list[MemoryEntry] = []
    for idx, turn in enumerate(interaction.turns, start=1):
        payload = turn.payload or {}
        if is_foreign_room_close_turn(payload, interaction.speaker_id):
            continue
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
