"""
Read-only query helpers and shared validators for RelationshipMemory.

All functions accept an open ``aiosqlite.Connection``, an ``agent_id``
string, and optional type parameters. They carry no object state and are
safe to call from any async context.
"""

from __future__ import annotations

import logging

import aiosqlite
from opentelemetry import trace

from ..observability.spans import RELATIONSHIP_LOOKUP_SPAN
from .relationship_types import (
    _DEFAULT_TRUST,
    _MAX_RECENT_INTERACTIONS,
    Interaction,
    RelationshipSummary,
)

logger = logging.getLogger(__name__)
_tracer = trace.get_tracer(__name__)

__all__ = [
    "get_all_relationships",
    "get_relationship_summary",
    "get_trust",
    "validate_other_id",
    "validate_participant_types",
    "truncate_field",
]


# ─── Shared validators & helpers ────────────────────────────────────────────


def validate_other_id(other_id: str) -> None:
    """Reject empty other_id (F-4-1)."""
    if not other_id or not other_id.strip():
        raise ValueError("other_id must not be empty")


def validate_participant_types(
    participant_type: str, other_participant_type: str,
) -> None:
    """Validate participant types at write boundary (OQ 3)."""
    from ..participant import validate_participant_type
    validate_participant_type(participant_type)
    validate_participant_type(other_participant_type)


def truncate_field(
    agent_id: str, value: str, other_id: str, label: str,
) -> str:
    """Cap a string field to 1024 chars to prevent unbounded storage."""
    if len(value) > 1024:
        logger.warning(
            "%s truncated from %d to 1024 chars for %s→%s",
            label, len(value), agent_id, other_id,
        )
        return value[:1021] + "..."
    return value


# ─── Read queries ────────────────────────────────────────────────────────────


async def get_trust(
    db: aiosqlite.Connection,
    agent_id: str,
    other_id: str,
    *,
    participant_type: str = "agent",
    other_participant_type: str = "agent",
) -> float:
    """Get current trust score for another participant (0.0–1.0).

    Returns the default (0.5) if no relationship exists.
    """
    attrs = {"agent.id": agent_id, "participant.id": other_id}
    with _tracer.start_as_current_span(RELATIONSHIP_LOOKUP_SPAN, attributes=attrs):
        async with db.execute(
            "SELECT trust_score FROM relationships "
            "WHERE participant_id = ? AND participant_type = ? "
            "AND other_participant_id = ? AND other_participant_type = ?",
            (agent_id, participant_type, other_id, other_participant_type),
        ) as cursor:
            row = await cursor.fetchone()
        return row[0] if row is not None else _DEFAULT_TRUST


async def get_relationship_summary(
    db: aiosqlite.Connection,
    agent_id: str,
    other_id: str,
    *,
    participant_type: str = "agent",
    other_participant_type: str = "agent",
) -> RelationshipSummary:
    """Get full relationship context for injection into LLM prompt."""
    # Fetch relationship row.
    async with db.execute(
        "SELECT trust_score, interaction_count, last_interaction_at, notes "
        "FROM relationships "
        "WHERE participant_id = ? AND participant_type = ? "
        "AND other_participant_id = ? AND other_participant_type = ?",
        (agent_id, participant_type, other_id, other_participant_type),
    ) as cursor:
        row = await cursor.fetchone()

    if row is None:
        return RelationshipSummary(
            other_participant_id=other_id,
            other_participant_type=other_participant_type,
            trust_score=_DEFAULT_TRUST,
            interaction_count=0,
            last_interaction_at=None,
            notes=None,
        )

    trust_score, interaction_count, last_interaction_at, notes = row

    # Fetch recent interactions.
    async with db.execute(
        "SELECT id, participant_id, participant_type, "
        "other_participant_id, other_participant_type, "
        "interaction_type, outcome, sentiment, created_at "
        "FROM interactions "
        "WHERE participant_id = ? AND participant_type = ? "
        "AND other_participant_id = ? AND other_participant_type = ? "
        "ORDER BY created_at DESC LIMIT ?",
        (agent_id, participant_type, other_id,
         other_participant_type, _MAX_RECENT_INTERACTIONS),
    ) as cursor:
        interaction_rows = await cursor.fetchall()

    recent = [
        Interaction(
            id=r[0],
            participant_id=r[1],
            participant_type=r[2],
            other_participant_id=r[3],
            other_participant_type=r[4],
            interaction_type=r[5],
            outcome=r[6],
            sentiment=r[7],
            created_at=r[8],
        )
        for r in interaction_rows
    ]

    return RelationshipSummary(
        other_participant_id=other_id,
        other_participant_type=other_participant_type,
        trust_score=trust_score,
        interaction_count=interaction_count,
        last_interaction_at=last_interaction_at,
        notes=notes,
        recent_interactions=recent,
    )


async def get_all_relationships(
    db: aiosqlite.Connection,
    agent_id: str,
    *,
    participant_type: str = "agent",
) -> list[RelationshipSummary]:
    """Get summaries for all known relationships of an agent.

    .. note::

       ``recent_interactions`` is not populated in returned summaries
       (defaults to ``[]``) to avoid N+1 queries. Use
       ``get_relationship_summary()`` for individual relationships
       with full interaction history.
    """
    async with db.execute(
        "SELECT other_participant_id, other_participant_type, "
        "trust_score, interaction_count, "
        "last_interaction_at, notes "
        "FROM relationships "
        "WHERE participant_id = ? AND participant_type = ? "
        "ORDER BY trust_score DESC",
        (agent_id, participant_type),
    ) as cursor:
        rows = await cursor.fetchall()

    return [
        RelationshipSummary(
            other_participant_id=r[0],
            other_participant_type=r[1],
            trust_score=r[2],
            interaction_count=r[3],
            last_interaction_at=r[4],
            notes=r[5],
        )
        for r in rows
    ]
