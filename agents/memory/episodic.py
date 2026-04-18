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
from .migrations import (
    _FTS5_DDL,
    _NOTES_FTS5_DDL,
    _apply_migrations,
    _fts5_available,
)
from .notes import Note, NoteStore

if TYPE_CHECKING:
    from ..llm_client import LLMClient

logger = logging.getLogger(__name__)


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
    ) -> list[Episode]:
        """Retrieve relevant episodes ranked by composite score.

        Uses FTS5 BM25 when available, falls back to LIKE.
        Increments access_count on returned entries.
        """
        if limit < 1:
            raise ValueError(f"limit must be >= 1, got {limit}")
        if limit > MAX_RECALL_LIMIT:
            logger.warning(
                "limit=%d exceeds maximum (%d), capping",
                limit, MAX_RECALL_LIMIT,
            )
            limit = MAX_RECALL_LIMIT
        db = self._ensure_db()

        if query and self._fts5:
            rows = await recall_fts5(db, self._agent_id, query, limit, min_importance)
        elif query:
            rows = await recall_like(db, self._agent_id, query, limit, min_importance)
        else:
            rows = await recall_recency(db, self._agent_id, limit, min_importance)

        episodes = [row_to_episode(row) for row in rows]

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

    # Maximum characters of serialised context to include in the
    # summarization prompt.  Prevents arbitrarily large episode context
    # dicts from blowing up LLM input size.
    _MAX_CONTEXT_CHARS = 2000

    async def summarize_old_episodes(
        self,
        older_than_days: float,
        llm_client: LLMClient,
        *,
        compression_model: str = "claude-haiku-4",
        batch_size: int = 50,
    ) -> int:
        """Summarize raw episodes older than *older_than_days*.

        Selects up to *batch_size* episodes with ``compression_level < 1``
        whose ``created_at`` is older than the threshold, calls the LLM to
        produce a compressed summary, then updates each episode in place.
        Each successful update is committed immediately so that progress is
        not lost if the process crashes mid-batch.

        Callers that need to process a full backlog should invoke this method
        in a loop until it returns 0.

        Not concurrency-safe.  External callers should ensure only one
        summarization run per agent at a time.

        Returns the number of episodes summarized in this batch.
        """
        if batch_size < 1:
            raise ValueError(f"batch_size must be >= 1, got {batch_size}")
        if older_than_days < 0:
            raise ValueError(f"older_than_days must be >= 0, got {older_than_days}")
        db = self._ensure_db()
        cutoff = time.time() - older_than_days * 86400.0

        # NOTE: compression_level < 1 intentionally limits selection to raw
        # (level-0) episodes.  The 1→2 ("distilled") transition defined in
        # the RFC is not yet reachable through this method.  A separate
        # distill_old_episodes() (or a max_compression_level parameter) is
        # planned for a future PR.
        #
        # LIMIT bounds the batch to avoid unbounded serial LLM calls and
        # memory usage for agents with large unsummarized backlogs.  Callers
        # should loop until this method returns 0.
        async with db.execute(
            f"SELECT {EPISODE_SELECT} FROM episodes "
            "WHERE agent_id = ? AND compression_level < 1 AND created_at < ? "
            "ORDER BY created_at ASC LIMIT ?",
            (self._agent_id, cutoff, batch_size),
        ) as cursor:
            rows = await cursor.fetchall()

        if not rows:
            return 0

        summarized = 0
        for row in rows:
            episode = row_to_episode(row)
            prompt = (
                f"Summarize the following episode concisely, preserving key facts "
                f"and outcomes.\n\n"
                f"Summary: {episode.summary}\n"
            )
            if episode.outcome:
                prompt += f"Outcome: {episode.outcome}\n"
            if episode.tags:
                prompt += f"Tags: {', '.join(episode.tags)}\n"
            if episode.context:
                ctx_str = json.dumps(episode.context)
                if len(ctx_str) > self._MAX_CONTEXT_CHARS:
                    ctx_str = ctx_str[: self._MAX_CONTEXT_CHARS] + "... [truncated]"
                prompt += f"Context: {ctx_str}\n"

            try:
                response = await llm_client.create_message(
                    model=compression_model,
                    messages=[{"role": "user", "content": prompt}],
                    system=(
                        "You are a concise summarizer. "
                        "Distill the episode into a brief summary."
                    ),
                    tools=[],
                    max_tokens=256,
                    temperature=0.2,
                )
                summary = response.text
                if response.usage:
                    logger.debug(
                        "Summarization tokens for episode %s: in=%d out=%d",
                        episode.id,
                        response.usage.input_tokens,
                        response.usage.output_tokens,
                    )
                if summary is None or not summary.strip():
                    logger.warning(
                        "Summarization of episode %s returned %s, skipping",
                        episode.id,
                        "no text" if summary is None else "empty text",
                    )
                    continue

                # Strip leading/trailing whitespace from LLM output (F-3c-1).
                summary = summary.strip()

                now = time.time()
                new_level = episode.compression_level + 1
                update_cursor = await db.execute(
                    "UPDATE episodes SET summary = ?, compression_level = ?, "
                    "compressed_at = ? WHERE id = ? AND agent_id = ?",
                    (summary, new_level, now, episode.id, self._agent_id),
                )
                if update_cursor.rowcount > 0:
                    # Commit each episode individually so that progress is
                    # durable even if the process crashes mid-batch.
                    await db.commit()
                    summarized += 1
                    logger.info(
                        "Summarized episode %s: compression_level %d → %d",
                        episode.id,
                        episode.compression_level,
                        new_level,
                    )
            except Exception:
                logger.warning(
                    "Failed to summarize episode %s", episode.id, exc_info=True,
                )

        return summarized

    async def delete_old_episodes(self, older_than_days: float) -> int:
        """Delete compressed episodes older than *older_than_days*.

        Only episodes with ``compression_level >= 1`` are eligible for
        deletion.  Uncompressed (raw) episodes are never deleted — they
        must be summarized first via :meth:`summarize_old_episodes`.

        Returns the number of episodes deleted.
        """
        if older_than_days < 0:
            raise ValueError(f"older_than_days must be >= 0, got {older_than_days}")
        db = self._ensure_db()
        cutoff = time.time() - older_than_days * 86400.0

        cursor = await db.execute(
            "DELETE FROM episodes "
            "WHERE agent_id = ? AND compression_level >= 1 AND created_at < ?",
            (self._agent_id, cutoff),
        )
        deleted = cursor.rowcount
        if deleted:
            await db.commit()
            logger.info(
                "Deleted %d compressed episodes older than %.1f days for agent %s",
                deleted,
                older_than_days,
                self._agent_id,
            )
        return deleted

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
    ) -> list[Note]:
        """Retrieve notes matching query, ranked by relevance.

        Increments access_count on returned notes.
        """
        return await self._ensure_note_store().recall_notes(query, limit=limit)

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
        """Increment and return the new interaction count."""
        return await increment_interaction_count(self._ensure_db(), self._agent_id)

    async def reset_interaction_count(self) -> None:
        """Reset the interaction counter to zero."""
        await reset_interaction_count(self._ensure_db(), self._agent_id)

    # ─── Persona state persistence ──────────────────────────

    async def persist_agent_state(
        self, agent_id: str, state_json: str,
    ) -> None:
        """Persist opaque agent state JSON to the agent_state table."""
        await persist_agent_state(self._ensure_db(), agent_id, state_json)

    async def load_agent_state(self, agent_id: str) -> str | None:
        """Load opaque agent state JSON from the agent_state table.

        Returns ``None`` if no state has been persisted for this agent.
        """
        return await load_agent_state(self._ensure_db(), agent_id)
