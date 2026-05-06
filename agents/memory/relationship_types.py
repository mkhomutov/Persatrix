"""Relationship memory data types and constants.

Pure data definitions with no database dependency — separated from
``relationship.py`` for clean import boundaries and file-size hygiene.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Maximum per-call trust delta — prevents single interactions from
# swinging trust dramatically.
_MAX_TRUST_DELTA = 0.2

# Default trust score for unknown agent pairs.
_DEFAULT_TRUST = 0.5

# Maximum number of recent interactions returned by get_relationship_summary().
_MAX_RECENT_INTERACTIONS = 10


@dataclass
class Interaction:
    """A single recorded interaction between two participants."""

    id: str
    participant_id: str
    participant_type: str
    other_participant_id: str
    other_participant_type: str
    interaction_type: str
    outcome: str | None
    sentiment: float
    created_at: float


@dataclass
class RelationshipSummary:
    """Summary of a relationship for LLM prompt injection."""

    other_participant_id: str
    other_participant_type: str
    trust_score: float
    interaction_count: int
    last_interaction_at: float | None
    notes: str | None
    recent_interactions: list[Interaction] = field(default_factory=list)
    # RFC 0021 PR 2: timestamp of the earliest stored interaction, used
    # to compute the relationship-cadence bucket ("frequent" / "regular"
    # / "sparse") in :mod:`agents.persona_runtime.memory_context`.
    # Computed via ``MIN(created_at)`` on the interactions table inside
    # ``get_relationship_summary``.  ``None`` for relationships that were
    # seeded from config without any recorded interaction yet, or for
    # legacy rows with no interaction history.
    first_interaction_at: float | None = None
