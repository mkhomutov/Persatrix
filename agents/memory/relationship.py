"""
Relationship memory — tracks per-agent-pair trust, interaction patterns, and history.

Shares the SQLite database and migration infrastructure with episodic memory.
Trust scores are bidirectionally decayed toward 0.5 (neutral) to prevent
permanent grudges in long-running simulations.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import aiosqlite

from .episodic import _apply_migrations

logger = logging.getLogger(__name__)

# Maximum per-call trust delta — prevents single interactions from
# swinging trust dramatically.
_MAX_TRUST_DELTA = 0.2

# Default trust score for unknown agent pairs.
_DEFAULT_TRUST = 0.5

# Maximum number of recent interactions returned by get_relationship_summary().
_MAX_RECENT_INTERACTIONS = 10


@dataclass
class Interaction:
    """A single recorded interaction between two agents."""

    id: str
    agent_id: str
    other_agent_id: str
    interaction_type: str
    outcome: str | None
    sentiment: float
    created_at: float


@dataclass
class RelationshipSummary:
    """Summary of a relationship for LLM prompt injection."""

    other_agent_id: str
    trust_score: float
    interaction_count: int
    last_interaction_at: float | None
    notes: str | None
    recent_interactions: list[Interaction] = field(default_factory=list)


class RelationshipMemory:
    """Per-agent-pair trust and interaction tracking.

    Stores trust scores and interaction history in SQLite, sharing the
    database file with ``EpisodicMemory``. Trust is scoped to
    ``(agent_id, other_agent_id)`` pairs.
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

    # ─── Trust CRUD ─────────────────────────────────────────

    async def get_trust(self, other_agent_id: str) -> float:
        """Get current trust score for another agent (0.0–1.0).

        Returns the default (0.5) if no relationship exists.
        """
        db = self._ensure_db()
        async with db.execute(
            "SELECT trust_score FROM relationships "
            "WHERE agent_id = ? AND other_agent_id = ?",
            (self._agent_id, other_agent_id),
        ) as cursor:
            row = await cursor.fetchone()
        return row[0] if row is not None else _DEFAULT_TRUST

    async def update_trust(
        self,
        other_agent_id: str,
        delta: float,
        reason: str,
    ) -> float:
        """Update trust score. Returns new value (clamped to [0.0, 1.0]).

        *delta* is clamped to ±0.2 to prevent single interactions from
        swinging trust dramatically.
        """
        db = self._ensure_db()
        # Clamp delta to prevent extreme single-event swings.
        delta = max(-_MAX_TRUST_DELTA, min(_MAX_TRUST_DELTA, delta))

        current = await self.get_trust(other_agent_id)
        new_trust = max(0.0, min(1.0, current + delta))
        now = time.time()

        await db.execute(
            """
            INSERT INTO relationships
                (agent_id, other_agent_id, trust_score, interaction_count,
                 last_interaction_at, notes)
            VALUES (?, ?, ?, 0, ?, ?)
            ON CONFLICT(agent_id, other_agent_id) DO UPDATE SET
                trust_score = ?,
                notes = ?
            """,
            (
                self._agent_id,
                other_agent_id,
                new_trust,
                now,
                reason,
                new_trust,
                reason,
            ),
        )
        await db.commit()
        logger.debug(
            "Trust %s→%s: %.3f → %.3f (delta=%.3f, reason=%s)",
            self._agent_id,
            other_agent_id,
            current,
            new_trust,
            delta,
            reason,
        )
        return new_trust

    async def apply_decay(self, decay_rate: float = 0.01) -> int:
        """Decay all trust scores toward 0.5 (neutral).

        Bidirectional: trust above 0.5 decays downward, trust below 0.5
        decays upward. Formula: ``new = old + decay_rate * (0.5 - old)``.

        Returns the number of relationships updated.
        """
        if not 0.0 < decay_rate <= 1.0:
            raise ValueError(f"decay_rate must be in (0.0, 1.0], got {decay_rate}")
        db = self._ensure_db()

        # Apply decay in a single UPDATE: trust + rate * (0.5 - trust)
        cursor = await db.execute(
            """
            UPDATE relationships
            SET trust_score = trust_score + ? * (0.5 - trust_score)
            WHERE agent_id = ?
              AND ABS(trust_score - 0.5) > 0.001
            """,
            (decay_rate, self._agent_id),
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
        other_agent_id: str,
        interaction_type: str,
        outcome: str | None = None,
        sentiment: float = 0.0,
    ) -> str:
        """Record an interaction with another agent.

        Inserts into the ``interactions`` table and increments the
        ``interaction_count`` on the relationship. Creates the
        relationship row if it doesn't exist.

        Returns the generated interaction ID.
        """
        db = self._ensure_db()
        interaction_id = str(uuid.uuid4())
        now = time.time()

        await db.execute(
            """
            INSERT INTO interactions
                (id, agent_id, other_agent_id, interaction_type,
                 outcome, sentiment, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                interaction_id,
                self._agent_id,
                other_agent_id,
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
                (agent_id, other_agent_id, trust_score, interaction_count,
                 last_interaction_at, notes)
            VALUES (?, ?, ?, 1, ?, NULL)
            ON CONFLICT(agent_id, other_agent_id) DO UPDATE SET
                interaction_count = interaction_count + 1,
                last_interaction_at = ?
            """,
            (
                self._agent_id,
                other_agent_id,
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
        other_agent_id: str,
    ) -> RelationshipSummary:
        """Get full relationship context for injection into LLM prompt."""
        db = self._ensure_db()

        # Fetch relationship row.
        async with db.execute(
            "SELECT trust_score, interaction_count, last_interaction_at, notes "
            "FROM relationships WHERE agent_id = ? AND other_agent_id = ?",
            (self._agent_id, other_agent_id),
        ) as cursor:
            row = await cursor.fetchone()

        if row is None:
            return RelationshipSummary(
                other_agent_id=other_agent_id,
                trust_score=_DEFAULT_TRUST,
                interaction_count=0,
                last_interaction_at=None,
                notes=None,
            )

        trust_score, interaction_count, last_interaction_at, notes = row

        # Fetch recent interactions.
        async with db.execute(
            "SELECT id, agent_id, other_agent_id, interaction_type, "
            "outcome, sentiment, created_at "
            "FROM interactions "
            "WHERE agent_id = ? AND other_agent_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (self._agent_id, other_agent_id, _MAX_RECENT_INTERACTIONS),
        ) as cursor:
            interaction_rows = await cursor.fetchall()

        recent = [
            Interaction(
                id=r[0],
                agent_id=r[1],
                other_agent_id=r[2],
                interaction_type=r[3],
                outcome=r[4],
                sentiment=r[5],
                created_at=r[6],
            )
            for r in interaction_rows
        ]

        return RelationshipSummary(
            other_agent_id=other_agent_id,
            trust_score=trust_score,
            interaction_count=interaction_count,
            last_interaction_at=last_interaction_at,
            notes=notes,
            recent_interactions=recent,
        )

    async def get_all_relationships(self) -> list[RelationshipSummary]:
        """Get summaries for all known relationships of this agent."""
        db = self._ensure_db()
        async with db.execute(
            "SELECT other_agent_id, trust_score, interaction_count, "
            "last_interaction_at, notes "
            "FROM relationships WHERE agent_id = ? "
            "ORDER BY trust_score DESC",
            (self._agent_id,),
        ) as cursor:
            rows = await cursor.fetchall()

        return [
            RelationshipSummary(
                other_agent_id=r[0],
                trust_score=r[1],
                interaction_count=r[2],
                last_interaction_at=r[3],
                notes=r[4],
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
                continue
            trust_level = max(0.0, min(1.0, float(trust_level)))

            # INSERT OR IGNORE — only seeds if no row exists.
            await db.execute(
                """
                INSERT OR IGNORE INTO relationships
                    (agent_id, other_agent_id, trust_score, interaction_count,
                     last_interaction_at, notes)
                VALUES (?, ?, ?, 0, NULL, NULL)
                """,
                (self._agent_id, other_id, trust_level),
            )
        await db.commit()
