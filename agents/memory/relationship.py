"""
Relationship memory — tracks per-agent-pair trust, interaction patterns, and history.

Shares the SQLite database and migration infrastructure with episodic memory.
Trust scores are bidirectionally decayed toward 0.5 (neutral) to prevent
permanent grudges in long-running simulations.

SQL query helpers live in :mod:`.relationship_queries`;
write/mutation helpers live in :mod:`.relationship_mutations`.
"""

from __future__ import annotations

import logging

import aiosqlite

from .migrations import _apply_migrations
from .relationship_mutations import (
    apply_decay as _apply_decay,
)
from .relationship_mutations import (
    record_interaction as _record_interaction,
)
from .relationship_mutations import (
    seed_trust as _seed_trust,
)
from .relationship_mutations import (
    update_trust as _update_trust,
)
from .relationship_queries import (
    get_all_relationships as _get_all_relationships,
)
from .relationship_queries import (
    get_relationship_summary as _get_relationship_summary,
)
from .relationship_queries import (
    get_trust as _get_trust,
)
from .relationship_types import (
    _DEFAULT_TRUST,
    _MAX_RECENT_INTERACTIONS,
    _MAX_TRUST_DELTA,
    Interaction,
    RelationshipSummary,
)

logger = logging.getLogger(__name__)

__all__ = [
    "RelationshipMemory",
    "Interaction",
    "RelationshipSummary",
    "_DEFAULT_TRUST",
    "_MAX_RECENT_INTERACTIONS",
    "_MAX_TRUST_DELTA",
]


class RelationshipMemory:
    """Per-participant-pair trust and interaction tracking.

    Stores trust scores and interaction history in SQLite, sharing the
    database file with ``EpisodicMemory``. Trust is scoped to
    ``(participant_id, participant_type, other_participant_id,
    other_participant_type)`` tuples.

    All methods accept optional ``participant_type`` and
    ``other_participant_type`` keyword arguments (default ``"agent"``).
    Existing callers are unaffected.
    """

    def __init__(self, agent_id: str, db_path: str = "data/memory.db") -> None:
        self._agent_id = agent_id
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    @property
    def agent_id(self) -> str:
        return self._agent_id

    async def initialize(
        self,
        config_relationships: list[dict[str, object]] | None = None,
    ) -> None:
        """Open database, run migrations, seed trust from config.

        *config_relationships* is the ``relationships`` list from the
        agent's YAML config (e.g. ``[{"agent_id": "mike", "trust_level": 0.9}]``).
        Existing trust scores are never overwritten — config only seeds
        the initial state.
        """
        # Guard against double-initialize: close any existing connection
        # to prevent file descriptor and SQLite connection leaks.
        if self._db is not None:
            await self._db.close()

        self._db = await aiosqlite.connect(self._db_path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await _apply_migrations(self._db)

        if config_relationships:
            await _seed_trust(self._db, self._agent_id, config_relationships)

    async def close(self) -> None:
        """Close the database connection."""
        if self._db is not None:
            await self._db.close()
            self._db = None

    def _ensure_db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError(
                "RelationshipMemory not initialized — call initialize() first"
            )
        return self._db

    # ─── Trust CRUD ─────────────────────────────────────────

    async def get_trust(
        self,
        other_id: str,
        *,
        participant_type: str = "agent",
        other_participant_type: str = "agent",
    ) -> float:
        """Get current trust score for another participant (0.0–1.0).

        Returns the default (0.5) if no relationship exists.
        """
        return await _get_trust(
            self._ensure_db(), self._agent_id, other_id,
            participant_type=participant_type,
            other_participant_type=other_participant_type,
        )

    async def update_trust(
        self,
        other_id: str,
        delta: float,
        reason: str,
        *,
        participant_type: str = "agent",
        other_participant_type: str = "agent",
    ) -> float:
        """Update trust score. Returns new value (clamped to [0.0, 1.0]).

        *delta* is clamped to ±0.2 to prevent single interactions from
        swinging trust dramatically.

        .. note::

           The *reason* overwrites the previous ``notes`` value on the
           relationship row — only the most recent trust-change reason
           is retained.
        """
        return await _update_trust(
            self._ensure_db(), self._agent_id, other_id, delta, reason,
            participant_type=participant_type,
            other_participant_type=other_participant_type,
        )

    async def apply_decay(
        self,
        decay_rate: float = 0.01,
        *,
        participant_type: str = "agent",
    ) -> int:
        """Decay all trust scores toward 0.5 (neutral).

        Formula: ``new = old + decay_rate * (0.5 - old)``.
        Rows within 0.001 of neutral are skipped.  Decays all
        ``other_participant_type`` values uniformly (PR #120 F-5).

        Returns the number of relationships updated.
        """
        return await _apply_decay(
            self._ensure_db(), self._agent_id, decay_rate,
            participant_type=participant_type,
        )

    # ─── Interaction recording ──────────────────────────────

    async def record_interaction(
        self,
        other_id: str,
        interaction_type: str,
        outcome: str | None = None,
        sentiment: float = 0.0,
        *,
        participant_type: str = "agent",
        other_participant_type: str = "agent",
        session_id: str = "legacy",
    ) -> str:
        """Record an interaction with another participant.

        Inserts into the ``interactions`` table and increments the
        ``interaction_count`` on the relationship. Creates the
        relationship row if it doesn't exist.

        ``session_id`` (RFC 0031 Phase 1; default ``"legacy"``) tags the
        relationship row on first-seen INSERT.  See
        :func:`agents.memory.relationship_mutations.record_interaction`
        for the per-row tagging contract.

        Returns the generated interaction ID.
        """
        return await _record_interaction(
            self._ensure_db(), self._agent_id, other_id, interaction_type,
            outcome, sentiment,
            participant_type=participant_type,
            other_participant_type=other_participant_type,
            session_id=session_id,
        )

    # ─── Queries ────────────────────────────────────────────

    async def get_relationship_summary(
        self,
        other_id: str,
        *,
        participant_type: str = "agent",
        other_participant_type: str = "agent",
    ) -> RelationshipSummary:
        """Get full relationship context for injection into LLM prompt."""
        return await _get_relationship_summary(
            self._ensure_db(), self._agent_id, other_id,
            participant_type=participant_type,
            other_participant_type=other_participant_type,
        )

    async def get_all_relationships(
        self,
        *,
        participant_type: str = "agent",
    ) -> list[RelationshipSummary]:
        """Get summaries for all known relationships of this agent.

        .. note::

           ``recent_interactions`` is not populated in returned summaries
           (defaults to ``[]``) to avoid N+1 queries. Use
           ``get_relationship_summary()`` for individual relationships
           with full interaction history.
        """
        return await _get_all_relationships(
            self._ensure_db(), self._agent_id,
            participant_type=participant_type,
        )
