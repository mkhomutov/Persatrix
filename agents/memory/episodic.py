"""
Episodic memory — long-term storage of past interactions.

Stores summaries of conversations, decisions, and outcomes in SQLite
with FTS5 full-text search for relevance-ranked retrieval.

Also provides agent-initiated note storage (migration v2) for
structured knowledge the agent chooses to persist, delegated to
:class:`~agents.memory.notes.NoteStore`.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from typing import TYPE_CHECKING, Any

import aiosqlite
from opentelemetry import trace

from ..observability.spans import (
    EPISODIC_RECALL_SPAN,
    EPISODIC_REMEMBER_SPAN,
)
from .episodic_queries import (
    EPISODE_SELECT,
    MAX_RECALL_LIMIT,
    Episode,
    get_interaction_count,
    increment_interaction_count,
    load_agent_state,
    persist_agent_state,
    recall_fts5,
    recall_like,
    recall_recency,
    reset_interaction_count,
    row_to_episode,
)
from .episodic_retention import (
    delete_old_episodes as _delete_old_episodes,
)
from .episodic_retention import (
    summarize_old_episodes as _summarize_old_episodes,
)
from .migrations import (
    _FTS5_DDL,
    _NOTES_FTS5_DDL,
    _apply_migrations,
    _fts5_available,
)
from .notes import Note, NoteStore

_tracer = trace.get_tracer(__name__)

if TYPE_CHECKING:
    from ..llm_client import LLMClient

logger = logging.getLogger(__name__)


# ─── Per-tier min_score defaults (RFC 0017 §C) ────────────────────────────────
# Calibrated against a representative FTS5 BM25 score distribution:
# queries with a clear topic match produce |rank| ≈ 1.5–4.0 (normalised ≈ 0.20–0.40);
# low-signal queries ("hi", empty TICK boilerplate) produce |rank| ≥ 5 or no rows
# (normalised ≤ 0.17).  Thresholds are conservative to avoid over-filtering;
# operators can tighten them via caller overrides without API changes.
#
# Public API (PR 6 — RFC 0017 PR 4 review finding 1): the persona runtime is
# already a consumer and RFC 0008's vector tier will be the third.  Public
# names avoid ``ruff PLC2701`` (``import-private-name``) at consumer sites
# and signal the cross-module contract.  The leading-underscore aliases are
# retained for one release as a deprecation shim; remove in v0.3.
DEFAULT_EPISODIC_MIN_SCORE: float = 0.20
DEFAULT_NOTES_MIN_SCORE: float = 0.20

# Deprecated underscore aliases — remove in v0.3 once external pins clear.
_DEFAULT_EPISODIC_MIN_SCORE: float = DEFAULT_EPISODIC_MIN_SCORE
_DEFAULT_NOTES_MIN_SCORE: float = DEFAULT_NOTES_MIN_SCORE


# ─── EpisodicMemory ────────────────────────────────────────


class EpisodicMemory:
    """Long-term memory store using SQLite with FTS5 search."""

    def __init__(self, agent_id: str, db_path: str = "data/memory.db") -> None:
        self._agent_id = agent_id
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None
        self._fts5: bool = False
        self._note_store: NoteStore | None = None

    @property
    def agent_id(self) -> str:
        return self._agent_id

    @property
    def has_fts5(self) -> bool:
        """Return True when the underlying SQLite build provides FTS5.

        Public, read-only view of the internal ``_fts5`` flag set during
        :meth:`initialize`.  Tests and operators that need to gate on FTS5
        availability should use this property instead of reaching into the
        private attribute.  (PR 6 — RFC 0017 PR 4 review finding 3.)
        """
        return self._fts5

    async def initialize(self) -> None:
        """Open database, run migrations, set up FTS5 if available."""
        self._db = await aiosqlite.connect(self._db_path)
        await self._db.execute("PRAGMA journal_mode=WAL")

        await _apply_migrations(self._db)

        # Verify that ln() is available — the scoring formula in _SCORE_EXPR
        # uses SQLite's ln() function, which requires SQLITE_ENABLE_MATH_FUNCTIONS
        # at compile time.  Python's bundled SQLite typically includes this, but
        # custom builds or Alpine musl-based Docker images may not.  Failing
        # here with a clear message is better than a cryptic error at recall time
        # (PR #59 review: ln() startup diagnostic).
        try:
            async with self._db.execute("SELECT ln(1)") as cursor:
                await cursor.fetchone()
        except sqlite3.OperationalError:
            raise RuntimeError(
                "SQLite ln() function not available — required by recall scoring. "
                "Ensure Python is built with SQLITE_ENABLE_MATH_FUNCTIONS."
            )

        self._fts5 = await _fts5_available(self._db)
        if self._fts5:
            await self._db.executescript(_FTS5_DDL)
            await self._db.executescript(_NOTES_FTS5_DDL)
            await self._db.commit()
            logger.info("FTS5 enabled for episodic memory")
        else:
            logger.warning(
                "FTS5 not available — falling back to LIKE-based queries. "
                "Performance degrades beyond ~1000 episodes per agent. "
                "Install a Python build with FTS5 support for production use."
            )

        self._note_store = NoteStore(
            agent_id=self._agent_id, db=self._db, fts5=self._fts5,
        )

    async def close(self) -> None:
        """Close the database connection."""
        if self._db is not None:
            await self._db.close()
            self._db = None
            self._note_store = None

    def _ensure_db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("EpisodicMemory not initialized — call initialize() first")
        return self._db

    def _ensure_note_store(self) -> NoteStore:
        if self._note_store is None:
            raise RuntimeError("EpisodicMemory not initialized — call initialize() first")
        return self._note_store

    # ─── Episode CRUD ──────────────────────────────────────

    async def store_episode(
        self,
        summary: str,
        context: dict[str, Any],
        outcome: str | None = None,
        importance: float = 0.5,
        tags: list[str] | None = None,
    ) -> str:
        """Store a new episode. Returns the generated episode ID."""
        with _tracer.start_as_current_span(
            EPISODIC_REMEMBER_SPAN,
            attributes={
                "agent.id": self._agent_id,
                "episode.kind": "episode",
            },
        ):
            db = self._ensure_db()
            if not summary or not summary.strip():
                raise ValueError("summary must not be empty")
            # Clamp importance to [0.0, 1.0] — the scoring formula assumes
            # non-negative values; negative importance would invert ranking.
            if not 0.0 <= importance <= 1.0:
                logger.warning(
                    "importance=%.4f out of [0.0, 1.0] range, clamping", importance,
                )
                importance = max(0.0, min(1.0, importance))
            episode_id = str(uuid.uuid4())
            now = time.time()
            await db.execute(
                """
                INSERT INTO episodes
                    (id, agent_id, summary, context_json, outcome,
                     importance, access_count, last_accessed_at,
                     tags_json, created_at, compressed_at, compression_level)
                VALUES (?, ?, ?, ?, ?, ?, 0, NULL, ?, ?, NULL, 0)
                """,
                (
                    episode_id,
                    self._agent_id,
                    summary,
                    json.dumps(context),
                    outcome,
                    importance,
                    json.dumps(tags or []),
                    now,
                ),
            )
            await db.commit()
            return episode_id

    async def recall(
        self,
        query: str = "",
        *,
        limit: int = 10,
        min_importance: float = 0.0,
        min_score: float | None = None,
    ) -> list[Episode]:
        """Retrieve relevant episodes ranked by composite score.

        Uses FTS5 BM25 when available, falls back to LIKE.
        Increments access_count on returned entries.

        Parameters
        ----------
        min_score:
            Optional relevance floor in ``[0, 1]`` applied to FTS5 BM25
            normalised scores.  ``None`` → no filtering (current behaviour).
            LIKE-fallback path ignores this parameter (all LIKE matches score
            ``1.0`` per RFC 0017 Section C).
        """
        if limit < 1:
            raise ValueError(f"limit must be >= 1, got {limit}")
        if limit > MAX_RECALL_LIMIT:
            logger.warning(
                "limit=%d exceeds maximum (%d), capping",
                limit, MAX_RECALL_LIMIT,
            )
            limit = MAX_RECALL_LIMIT
        # Validate min_score range — RFC 0017 §C specifies [0.0, 1.0].
        # Out-of-range values silently no-op (negative) or filter everything
        # (>1.0), making misconfiguration hard to debug in production.
        # Mirrors the existing `limit` guard above (PR #147 review).
        if min_score is not None and not 0.0 <= min_score <= 1.0:
            raise ValueError(
                f"min_score must be in [0.0, 1.0] or None, got {min_score}"
            )
        with _tracer.start_as_current_span(
            EPISODIC_RECALL_SPAN,
            attributes={
                "agent.id": self._agent_id,
                "query.kind": "recall",
                "query.empty": not query,
                "min_score": -1.0 if min_score is None else min_score,
            },
        ) as span:
            db = self._ensure_db()

            if query and self._fts5:
                rows = await recall_fts5(
                    db, self._agent_id, query, limit, min_importance, min_score,
                )
            elif query:
                rows = await recall_like(
                    db, self._agent_id, query, limit, min_importance, min_score,
                )
            else:
                rows = await recall_recency(db, self._agent_id, limit, min_importance)

            episodes = [row_to_episode(row) for row in rows]
            span.set_attribute("result.count", len(episodes))

            # Increment access_count and update last_accessed_at
            if episodes:
                now = time.time()
                ids = [e.id for e in episodes]
                placeholders = ",".join("?" for _ in ids)
                await db.execute(
                    f"UPDATE episodes SET access_count = access_count + 1, "
                    f"last_accessed_at = ? WHERE id IN ({placeholders})",
                    [now, *ids],
                )
                await db.commit()
                # Update in-memory objects to reflect the increment
                for ep in episodes:
                    ep.access_count += 1
                    ep.last_accessed_at = now

            return episodes

    async def get_episode(self, episode_id: str) -> Episode | None:
        """Retrieve a single episode by ID (agent-scoped)."""
        db = self._ensure_db()
        async with db.execute(
            f"SELECT {EPISODE_SELECT} FROM episodes WHERE id = ? AND agent_id = ?",
            (episode_id, self._agent_id),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return row_to_episode(row)

    async def count_episodes(self) -> int:
        """Return the number of episodes for this agent."""
        db = self._ensure_db()
        async with db.execute(
            "SELECT COUNT(*) FROM episodes WHERE agent_id = ?",
            (self._agent_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return row[0] if row else 0

    # ─── Episode summarization & retention ──────────────────

    async def summarize_old_episodes(
        self,
        older_than_days: float,
        llm_client: LLMClient,
        *,
        compression_model: str = "claude-haiku-4",
        batch_size: int = 50,
    ) -> int:
        """Summarize raw episodes older than *older_than_days*.

        See :func:`~agents.memory.episodic_retention.summarize_old_episodes`
        for full documentation.
        """
        return await _summarize_old_episodes(
            self._ensure_db(), self._agent_id, older_than_days, llm_client,
            compression_model=compression_model, batch_size=batch_size,
        )

    async def delete_old_episodes(self, older_than_days: float) -> int:
        """Delete compressed episodes older than *older_than_days*.

        See :func:`~agents.memory.episodic_retention.delete_old_episodes`
        for full documentation.
        """
        return await _delete_old_episodes(
            self._ensure_db(), self._agent_id, older_than_days,
        )

    # ─── Notes (delegated to NoteStore) ────────────────────

    async def store_note(
        self,
        topic: str,
        content: str,
        tags: list[str] | None = None,
        max_notes: int = 500,
    ) -> str:
        """Store a new note. Prunes oldest low-access notes if over cap.

        Returns the generated note ID.
        """
        return await self._ensure_note_store().store_note(
            topic, content, tags=tags, max_notes=max_notes,
        )

    async def recall_notes(
        self,
        query: str = "",
        *,
        limit: int = 10,
        min_score: float | None = None,
    ) -> list[Note]:
        """Retrieve notes matching query, ranked by relevance.

        Increments access_count on returned notes.

        Parameters
        ----------
        min_score:
            Optional relevance floor in ``[0, 1]`` applied to FTS5 BM25
            normalised scores.  ``None`` → no filtering (current behaviour).
            LIKE-fallback path ignores this parameter per RFC 0017 Section C.
        """
        # Mirror the ``recall()`` validation at the public façade so a
        # future ``NoteStore`` refactor that drops its own guard cannot
        # silently lose validation.  (PR 6 — RFC 0017 PR 3 review finding 1.)
        if min_score is not None and not 0.0 <= min_score <= 1.0:
            raise ValueError(
                f"min_score must be in [0.0, 1.0] or None, got {min_score}"
            )
        return await self._ensure_note_store().recall_notes(query, limit=limit, min_score=min_score)

    async def update_note(self, note_id: str, content: str) -> bool:
        """Update note content. Topic and tags preserved. Returns True if found."""
        return await self._ensure_note_store().update_note(note_id, content)

    async def delete_note(self, note_id: str) -> bool:
        """Delete a note by ID (agent-scoped). Returns True if found."""
        return await self._ensure_note_store().delete_note(note_id)

    async def count_notes(self) -> int:
        """Return the number of notes for this agent."""
        return await self._ensure_note_store().count_notes()

    # ─── Interaction counter ─────────────────────────────────

    async def get_interaction_count(self) -> int:
        """Get the current interaction count for this agent."""
        return await get_interaction_count(self._ensure_db(), self._agent_id)

    async def increment_interaction_count(self) -> int:
        """Increment and return the new interaction count (upsert).

        Uses RETURNING to get the post-upsert count in a single round-trip,
        eliminating a read-after-write race.  Requires SQLite >= 3.35
        (Python 3.11+ ships >= 3.39).
        """
        return await increment_interaction_count(self._ensure_db(), self._agent_id)

    async def reset_interaction_count(self) -> None:
        """Reset the interaction counter to zero."""
        await reset_interaction_count(self._ensure_db(), self._agent_id)

    # ─── Persona state persistence ──────────────────────────

    async def persist_agent_state(
        self, agent_id: str, state_json: str,
    ) -> None:
        """Persist opaque agent state JSON to the agent_state table (upsert).

        Preserves interaction_count: only persona_state_json and updated_at
        are overwritten by the upsert, so call-count tracking is not reset.
        """
        await persist_agent_state(self._ensure_db(), agent_id, state_json)

    async def load_agent_state(self, agent_id: str) -> str | None:
        """Load opaque agent state JSON from the agent_state table.

        Returns ``None`` if no state has been persisted for this agent.
        """
        return await load_agent_state(self._ensure_db(), agent_id)
