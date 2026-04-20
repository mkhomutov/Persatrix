"""
Relationship memory — tracks per-agent-pair trust, interaction patterns, and history.

Shares the SQLite database and migration infrastructure with episodic memory.
Trust scores are bidirectionally decayed toward 0.5 (neutral) to prevent
permanent grudges in long-running simulations.
"""

from __future__ import annotations

import logging
import math
import time
import uuid
from typing import Any

import aiosqlite

from .migrations import _apply_migrations
from .relationship_types import (
    _DEFAULT_TRUST,
    _MAX_RECENT_INTERACTIONS,
    _MAX_TRUST_DELTA,
    Interaction,
    RelationshipSummary,
)

logger = logging.getLogger(__name__)


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
        config_relationships: list[dict[str, Any]] | None = None,
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
            await self._seed_trust(config_relationships)

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

    @staticmethod
    def _validate_other_id(other_id: str) -> None:
        """Reject empty other_id (F-4-1)."""
        if not other_id or not other_id.strip():
            raise ValueError("other_id must not be empty")

    @staticmethod
    def _validate_participant_types(
        participant_type: str, other_participant_type: str,
    ) -> None:
        """Validate participant types at write boundary (OQ 3)."""
        from ..participant import validate_participant_type
        validate_participant_type(participant_type)
        validate_participant_type(other_participant_type)

    def _truncate_field(
        self, value: str, other_id: str, label: str,
    ) -> str:
        """Cap a string field to 1024 chars to prevent unbounded storage."""
        if len(value) > 1024:
            logger.warning(
                "%s truncated from %d to 1024 chars for %s→%s",
                label, len(value), self._agent_id, other_id,
            )
            return value[:1021] + "..."
        return value

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
        db = self._ensure_db()
        async with db.execute(
            "SELECT trust_score FROM relationships "
            "WHERE participant_id = ? AND participant_type = ? "
            "AND other_participant_id = ? AND other_participant_type = ?",
            (self._agent_id, participant_type, other_id, other_participant_type),
        ) as cursor:
            row = await cursor.fetchone()
        return row[0] if row is not None else _DEFAULT_TRUST

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
        db = self._ensure_db()
        self._validate_other_id(other_id)
        self._validate_participant_types(participant_type, other_participant_type)
        if math.isnan(delta) or math.isinf(delta):
            raise ValueError(f"delta must be a finite number, got {delta}")
        delta = max(-_MAX_TRUST_DELTA, min(_MAX_TRUST_DELTA, delta))
        reason = self._truncate_field(reason, other_id, "reason")

        insert_trust = max(0.0, min(1.0, _DEFAULT_TRUST + delta))

        # SQL-level arithmetic in ON CONFLICT avoids TOCTOU race.
        # RETURNING avoids a separate get_trust() round-trip (SQLite >= 3.35).
        cursor = await db.execute(
            """
            INSERT INTO relationships
                (participant_id, participant_type,
                 other_participant_id, other_participant_type,
                 trust_score, interaction_count,
                 last_interaction_at, notes)
            VALUES (?, ?, ?, ?, ?, 0, NULL, ?)
            ON CONFLICT(participant_id, participant_type,
                        other_participant_id, other_participant_type) DO UPDATE SET
                trust_score = MAX(0.0, MIN(1.0, relationships.trust_score + ?)),
                notes = ?
            RETURNING trust_score
            """,
            (
                self._agent_id,
                participant_type,
                other_id,
                other_participant_type,
                insert_trust,
                reason,
                delta,
                reason,
            ),
        )
        row = await cursor.fetchone()
        await db.commit()

        new_trust = row[0]
        logger.debug(
            "Trust %s→%s: %.3f (delta=%.3f, reason=%s)",
            self._agent_id,
            other_id,
            new_trust,
            delta,
            reason,
        )
        return new_trust

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
        if math.isnan(decay_rate) or math.isinf(decay_rate) or not 0.0 < decay_rate <= 1.0:
            raise ValueError(f"decay_rate must be in (0.0, 1.0], got {decay_rate}")
        db = self._ensure_db()
        cursor = await db.execute(
            """
            UPDATE relationships
            SET trust_score = trust_score + ? * (0.5 - trust_score)
            WHERE participant_id = ? AND participant_type = ?
              AND ABS(trust_score - 0.5) > 0.001
            """,
            (decay_rate, self._agent_id, participant_type),
        )
        updated = cursor.rowcount
        if updated:
            await db.commit()
            logger.debug(
                "Applied trust decay (rate=%.3f) to %d relationships for %s",
                decay_rate,
                updated,
                self._agent_id,
            )
        return updated

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
    ) -> str:
        """Record an interaction with another participant.

        Inserts into the ``interactions`` table and increments the
        ``interaction_count`` on the relationship. Creates the
        relationship row if it doesn't exist.

        Returns the generated interaction ID.
        """
        db = self._ensure_db()

        self._validate_other_id(other_id)
        self._validate_participant_types(participant_type, other_participant_type)
        if not interaction_type or not interaction_type.strip():
            raise ValueError("interaction_type must not be empty")
        if math.isnan(sentiment) or math.isinf(sentiment):
            raise ValueError(f"sentiment must be a finite number, got {sentiment}")
        sentiment = max(-1.0, min(1.0, sentiment))

        if outcome:
            outcome = self._truncate_field(outcome, other_id, "outcome")

        interaction_id = str(uuid.uuid4())
        now = time.time()

        await db.execute(
            """
            INSERT INTO interactions
                (id, participant_id, participant_type,
                 other_participant_id, other_participant_type,
                 interaction_type, outcome, sentiment, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                interaction_id,
                self._agent_id,
                participant_type,
                other_id,
                other_participant_type,
                interaction_type,
                outcome,
                sentiment,
                now,
            ),
        )

        # Upsert relationship row: create if missing, increment count.
        await db.execute(
            """
            INSERT INTO relationships
                (participant_id, participant_type,
                 other_participant_id, other_participant_type,
                 trust_score, interaction_count,
                 last_interaction_at, notes)
            VALUES (?, ?, ?, ?, ?, 1, ?, NULL)
            ON CONFLICT(participant_id, participant_type,
                        other_participant_id, other_participant_type) DO UPDATE SET
                interaction_count = interaction_count + 1,
                last_interaction_at = ?
            """,
            (
                self._agent_id,
                participant_type,
                other_id,
                other_participant_type,
                _DEFAULT_TRUST,
                now,
                now,
            ),
        )
        await db.commit()
        return interaction_id

    # ─── Queries ────────────────────────────────────────────

    async def get_relationship_summary(
        self,
        other_id: str,
        *,
        participant_type: str = "agent",
        other_participant_type: str = "agent",
    ) -> RelationshipSummary:
        """Get full relationship context for injection into LLM prompt."""
        db = self._ensure_db()

        # Fetch relationship row.
        async with db.execute(
            "SELECT trust_score, interaction_count, last_interaction_at, notes "
            "FROM relationships "
            "WHERE participant_id = ? AND participant_type = ? "
            "AND other_participant_id = ? AND other_participant_type = ?",
            (self._agent_id, participant_type, other_id, other_participant_type),
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
            (self._agent_id, participant_type, other_id,
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
        db = self._ensure_db()
        async with db.execute(
            "SELECT other_participant_id, other_participant_type, "
            "trust_score, interaction_count, "
            "last_interaction_at, notes "
            "FROM relationships "
            "WHERE participant_id = ? AND participant_type = ? "
            "ORDER BY trust_score DESC",
            (self._agent_id, participant_type),
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

    # ─── Trust bootstrapping ───────────────────────────────

    async def _seed_trust(
        self,
        config_relationships: list[dict[str, Any]],
    ) -> None:
        """Seed trust scores from agent config. Never overwrites existing rows."""
        db = self._ensure_db()
        for entry in config_relationships:
            other_id = entry.get("agent_id")
            trust_level = entry.get("trust_level")
            if other_id is None or trust_level is None:
                logger.debug(
                    "Skipping config relationship entry: "
                    "missing agent_id or trust_level: %r",
                    entry,
                )
                continue
            try:
                trust_level = max(0.0, min(1.0, float(trust_level)))
            except (TypeError, ValueError):
                logger.warning(
                    "Invalid trust_level for %s: %r, skipping",
                    other_id,
                    trust_level,
                )
                continue

            # INSERT OR IGNORE — only seeds if no row exists.
            # Types hardcoded to 'agent': config schema only has agent_id
            # entries (PR #120 F-6).
            await db.execute(
                """
                INSERT OR IGNORE INTO relationships
                    (participant_id, participant_type,
                     other_participant_id, other_participant_type,
                     trust_score, interaction_count,
                     last_interaction_at, notes)
                VALUES (?, 'agent', ?, 'agent', ?, 0, NULL, NULL)
                """,
                (self._agent_id, other_id, trust_level),
            )
        await db.commit()
