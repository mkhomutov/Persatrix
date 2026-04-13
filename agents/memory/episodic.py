"""
Episodic memory — long-term storage of past interactions.

Stores summaries of conversations, decisions, and outcomes in SQLite
with FTS5 full-text search for relevance-ranked retrieval.

Also provides agent-initiated note storage (migration v2) for
structured knowledge the agent chooses to persist.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import aiosqlite

if TYPE_CHECKING:
    from ..llm_client import LLMClient

logger = logging.getLogger(__name__)


# ─── Data model ─────────────────────────────────────────────


@dataclass
class Episode:
    """A single episodic memory entry."""

    id: str
    agent_id: str
    summary: str
    context: dict[str, Any]
    outcome: str | None
    importance: float
    access_count: int
    last_accessed_at: float | None
    tags: list[str]
    created_at: float
    compressed_at: float | None
    compression_level: int  # 0=raw, 1=summarized, 2=distilled


@dataclass
class Note:
    """An agent-initiated note persisted via memory tools."""

    id: str
    agent_id: str
    topic: str
    content: str
    tags: list[str] = field(default_factory=list)
    access_count: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0


# Maximum content size for a single note (10 KB).
_MAX_NOTE_CONTENT_BYTES = 10_240

# Column list for SELECT queries on the notes table.
_NOTE_COLS = (
    "id", "agent_id", "topic", "content", "tags_json",
    "access_count", "created_at", "updated_at",
)
_NOTE_SELECT = ", ".join(_NOTE_COLS)

# Column list for SELECT queries — keeps _row_to_episode() positional
# mapping stable when future migrations add columns to the episodes table.
_EPISODE_COLS = (
    "id", "agent_id", "summary", "context_json", "outcome",
    "importance", "access_count", "last_accessed_at",
    "tags_json", "created_at", "compressed_at", "compression_level",
)
_EPISODE_SELECT = ", ".join(_EPISODE_COLS)
_EPISODE_SELECT_ALIASED = ", ".join(f"e.{c}" for c in _EPISODE_COLS)

# Maximum number of episodes returned by recall() to prevent unbounded
# result sets and resource exhaustion.
_MAX_RECALL_LIMIT = 100

# ─── Shared scoring SQL fragments ──────────────────────────

# Non-BM25 scoring components shared across _recall_fts5(), _recall_like(),
# and _recall_recency().  Extracted to avoid maintaining the same formula in
# three SQL strings (F-3a-2).
#
# importance is wrapped with (0.1 + importance * 0.9) so that episodes with
# importance=0.0 still receive a non-zero score (10% baseline) instead of
# being invisible in ranked recall (F-3a-1).
_IMPORTANCE_EXPR = "(0.1 + e.importance * 0.9)"
_ACCESS_BOOST_EXPR = "(1.0 + ln(1 + e.access_count))"
_RECENCY_DECAY_EXPR = "(1.0 / (1 + (? - e.created_at) / 86400.0))"
_SCORE_EXPR = f"{_IMPORTANCE_EXPR} * {_ACCESS_BOOST_EXPR} * {_RECENCY_DECAY_EXPR}"

# Same as _SCORE_EXPR but without the alias prefix for non-JOIN queries.
_IMPORTANCE_EXPR_BARE = "(0.1 + importance * 0.9)"
_ACCESS_BOOST_EXPR_BARE = "(1.0 + ln(1 + access_count))"
_RECENCY_DECAY_EXPR_BARE = "(1.0 / (1 + (? - created_at) / 86400.0))"
_SCORE_EXPR_BARE = (
    f"{_IMPORTANCE_EXPR_BARE} * {_ACCESS_BOOST_EXPR_BARE}"
    f" * {_RECENCY_DECAY_EXPR_BARE}"
)


# ─── Schema migrations ─────────────────────────────────────

# Forward-only migrations: (version, description, SQL).
# Each migration's SQL may contain multiple statements separated by ";".
MIGRATIONS: list[tuple[int, str, str]] = [
    (
        1,
        "Initial schema: episodes + agent_state + FTS5",
        """
        CREATE TABLE IF NOT EXISTS episodes (
            id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL,
            summary TEXT NOT NULL,
            context_json TEXT,
            outcome TEXT,
            importance REAL DEFAULT 0.5,
            access_count INTEGER DEFAULT 0,
            last_accessed_at REAL,
            tags_json TEXT,
            created_at REAL NOT NULL,
            compressed_at REAL,
            compression_level INTEGER DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_episodes_agent
            ON episodes(agent_id);
        CREATE INDEX IF NOT EXISTS idx_episodes_importance
            ON episodes(importance DESC);
        CREATE INDEX IF NOT EXISTS idx_episodes_created
            ON episodes(created_at DESC);

        CREATE TABLE IF NOT EXISTS agent_state (
            agent_id TEXT PRIMARY KEY,
            interaction_count INTEGER DEFAULT 0,
            persona_state_json TEXT,
            updated_at REAL NOT NULL
        );
        """,
    ),
    (
        2,
        "Notes table, FTS5 index, and sync triggers",
        """
        CREATE TABLE IF NOT EXISTS notes (
            id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL,
            topic TEXT NOT NULL,
            content TEXT NOT NULL,
            tags_json TEXT,
            access_count INTEGER DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_notes_agent
            ON notes(agent_id);
        CREATE INDEX IF NOT EXISTS idx_notes_topic
            ON notes(agent_id, topic);
        CREATE INDEX IF NOT EXISTS idx_notes_created
            ON notes(created_at DESC);
        """,
    ),
    (
        3,
        "Relationships and interactions tables",
        """
        CREATE TABLE IF NOT EXISTS relationships (
            agent_id TEXT NOT NULL,
            other_agent_id TEXT NOT NULL,
            trust_score REAL DEFAULT 0.5,
            interaction_count INTEGER DEFAULT 0,
            last_interaction_at REAL,
            notes TEXT,
            PRIMARY KEY (agent_id, other_agent_id)
        );

        CREATE TABLE IF NOT EXISTS interactions (
            id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL,
            other_agent_id TEXT NOT NULL,
            interaction_type TEXT NOT NULL,
            outcome TEXT,
            sentiment REAL DEFAULT 0.0,
            created_at REAL NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_relationships_agent
            ON relationships(agent_id);
        -- Composite covering index for get_relationship_summary() query:
        -- WHERE agent_id=? AND other_agent_id=? ORDER BY created_at DESC LIMIT N
        -- Replaces separate agent and created_at indexes; the composite
        -- index satisfies both the WHERE filter and ORDER BY in a single
        -- index scan, avoiding a temp sort.
        CREATE INDEX IF NOT EXISTS idx_interactions_lookup
            ON interactions(agent_id, other_agent_id, created_at DESC);
        """,
    ),
]

# FTS5 DDL — applied only when FTS5 is available.
_FTS5_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts USING fts5(
    summary, context_json,
    content=episodes, content_rowid=rowid
);

CREATE TRIGGER IF NOT EXISTS episodes_ai AFTER INSERT ON episodes BEGIN
    INSERT INTO episodes_fts(rowid, summary, context_json)
        VALUES (new.rowid, new.summary, new.context_json);
END;

CREATE TRIGGER IF NOT EXISTS episodes_ad AFTER DELETE ON episodes BEGIN
    INSERT INTO episodes_fts(episodes_fts, rowid, summary, context_json)
        VALUES ('delete', old.rowid, old.summary, old.context_json);
END;

CREATE TRIGGER IF NOT EXISTS episodes_au AFTER UPDATE ON episodes BEGIN
    INSERT INTO episodes_fts(episodes_fts, rowid, summary, context_json)
        VALUES ('delete', old.rowid, old.summary, old.context_json);
    INSERT INTO episodes_fts(rowid, summary, context_json)
        VALUES (new.rowid, new.summary, new.context_json);
END;
"""

# FTS5 DDL for notes — applied only when FTS5 is available.
_NOTES_FTS5_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
    topic, content, tags_json,
    content=notes, content_rowid=rowid
);

CREATE TRIGGER IF NOT EXISTS notes_ai AFTER INSERT ON notes BEGIN
    INSERT INTO notes_fts(rowid, topic, content, tags_json)
        VALUES (new.rowid, new.topic, new.content, new.tags_json);
END;

CREATE TRIGGER IF NOT EXISTS notes_ad AFTER DELETE ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, topic, content, tags_json)
        VALUES ('delete', old.rowid, old.topic, old.content, old.tags_json);
END;

CREATE TRIGGER IF NOT EXISTS notes_au AFTER UPDATE ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, topic, content, tags_json)
        VALUES ('delete', old.rowid, old.topic, old.content, old.tags_json);
    INSERT INTO notes_fts(rowid, topic, content, tags_json)
        VALUES (new.rowid, new.topic, new.content, new.tags_json);
END;
"""


async def _apply_migrations(db: aiosqlite.Connection) -> None:
    """Apply all pending schema migrations."""
    await db.execute(
        "CREATE TABLE IF NOT EXISTS schema_version "
        "(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL, description TEXT)"
    )
    async with db.execute("SELECT MAX(version) FROM schema_version") as cursor:
        row = await cursor.fetchone()
    current = (row[0] if row and row[0] is not None else 0)

    for version, desc, sql in MIGRATIONS:
        if version > current:
            # NOTE: executescript() implicitly calls COMMIT before executing,
            # so the DDL and the version record below are NOT atomic.  If the
            # process crashes between executescript() and the INSERT, the
            # migration is applied but not recorded — causing a re-run on
            # restart.  This is safe for v1 because all statements use
            # IF NOT EXISTS guards.  Future non-idempotent migrations (ALTER
            # TABLE, data transforms) MUST use individual db.execute() calls
            # inside a manually managed transaction instead.
            await db.executescript(sql)
            await db.execute(
                "INSERT INTO schema_version VALUES (?, ?, ?)",
                (version, time.time(), desc),
            )
            logger.info("Applied migration v%d: %s", version, desc)
    await db.commit()


async def _fts5_available(db: aiosqlite.Connection) -> bool:
    """Test FTS5 availability with a throwaway virtual table."""
    try:
        await db.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS _fts5_test USING fts5(x)"
        )
        await db.execute("DROP TABLE IF EXISTS _fts5_test")
        return True
    except Exception:
        return False


# ─── EpisodicMemory ────────────────────────────────────────


class EpisodicMemory:
    """Long-term memory store using SQLite with FTS5 search."""

    def __init__(self, agent_id: str, db_path: str = "data/memory.db") -> None:
        self._agent_id = agent_id
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None
        self._fts5: bool = False

    @property
    def agent_id(self) -> str:
        return self._agent_id

    async def initialize(self) -> None:
        """Open database, run migrations, set up FTS5 if available."""
        self._db = await aiosqlite.connect(self._db_path)
        await self._db.execute("PRAGMA journal_mode=WAL")

        await _apply_migrations(self._db)

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

    async def close(self) -> None:
        """Close the database connection."""
        if self._db is not None:
            await self._db.close()
            self._db = None

    def _ensure_db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("EpisodicMemory not initialized — call initialize() first")
        return self._db

    # ─── CRUD ───────────────────────────────────────────────

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
        if limit > _MAX_RECALL_LIMIT:
            logger.warning(
                "limit=%d exceeds maximum (%d), capping",
                limit, _MAX_RECALL_LIMIT,
            )
            limit = _MAX_RECALL_LIMIT
        db = self._ensure_db()

        if query and self._fts5:
            rows = await self._recall_fts5(db, query, limit, min_importance)
        elif query:
            rows = await self._recall_like(db, query, limit, min_importance)
        else:
            rows = await self._recall_recency(db, limit, min_importance)

        episodes = [self._row_to_episode(row) for row in rows]

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

    async def _recall_fts5(
        self,
        db: aiosqlite.Connection,
        query: str,
        limit: int,
        min_importance: float,
    ) -> list[aiosqlite.Row]:
        """FTS5 search with composite BM25 x importance x access x recency scoring.

        Falls back to LIKE search if the query contains malformed FTS5 syntax
        (e.g. lone ``*``, unbalanced quotes, bare ``NOT``).
        """
        try:
            async with db.execute(
                f"""
                SELECT {_EPISODE_SELECT_ALIASED}
                FROM episodes_fts fts
                JOIN episodes e ON e.rowid = fts.rowid
                WHERE episodes_fts MATCH ?
                  AND e.agent_id = ?
                  AND e.importance >= ?
                ORDER BY
                    (fts.rank * -1)
                    * {_SCORE_EXPR}
                    DESC
                LIMIT ?
                """,
                (query, self._agent_id, min_importance, time.time(), limit),
            ) as cursor:
                return await cursor.fetchall()
        except sqlite3.OperationalError:
            logger.warning(
                "FTS5 query failed for %r, falling back to LIKE", query,
            )
            return await self._recall_like(db, query, limit, min_importance)

    async def _recall_like(
        self,
        db: aiosqlite.Connection,
        query: str,
        limit: int,
        min_importance: float,
    ) -> list[aiosqlite.Row]:
        """LIKE fallback when FTS5 is unavailable.

        Escapes LIKE wildcard characters (``%``, ``_``) in the query so they
        are matched literally rather than treated as pattern metacharacters.
        """
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        async with db.execute(
            f"""
            SELECT {_EPISODE_SELECT}
            FROM episodes
            WHERE agent_id = ?
              AND importance >= ?
              AND (summary LIKE ? ESCAPE '\\' OR context_json LIKE ? ESCAPE '\\')
            ORDER BY
                {_SCORE_EXPR_BARE}
                DESC
            LIMIT ?
            """,
            (self._agent_id, min_importance, pattern, pattern, time.time(), limit),
        ) as cursor:
            return await cursor.fetchall()

    async def _recall_recency(
        self,
        db: aiosqlite.Connection,
        limit: int,
        min_importance: float,
    ) -> list[aiosqlite.Row]:
        """No query text — rank by importance x access x recency only."""
        async with db.execute(
            f"""
            SELECT {_EPISODE_SELECT}
            FROM episodes
            WHERE agent_id = ?
              AND importance >= ?
            ORDER BY
                {_SCORE_EXPR_BARE}
                DESC
            LIMIT ?
            """,
            (self._agent_id, min_importance, time.time(), limit),
        ) as cursor:
            return await cursor.fetchall()

    def _row_to_episode(self, row: aiosqlite.Row) -> Episode:
        """Convert a database row to an Episode dataclass."""
        return Episode(
            id=row[0],
            agent_id=row[1],
            summary=row[2],
            context=json.loads(row[3]) if row[3] else {},
            outcome=row[4],
            importance=row[5],
            access_count=row[6],
            last_accessed_at=row[7],
            tags=json.loads(row[8]) if row[8] else [],
            created_at=row[9],
            compressed_at=row[10],
            compression_level=row[11],
        )

    async def get_episode(self, episode_id: str) -> Episode | None:
        """Retrieve a single episode by ID (agent-scoped)."""
        db = self._ensure_db()
        async with db.execute(
            f"SELECT {_EPISODE_SELECT} FROM episodes WHERE id = ? AND agent_id = ?",
            (episode_id, self._agent_id),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return self._row_to_episode(row)

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
            f"SELECT {_EPISODE_SELECT} FROM episodes "
            "WHERE agent_id = ? AND compression_level < 1 AND created_at < ? "
            "ORDER BY created_at ASC LIMIT ?",
            (self._agent_id, cutoff, batch_size),
        ) as cursor:
            rows = await cursor.fetchall()

        if not rows:
            return 0

        summarized = 0
        for row in rows:
            episode = self._row_to_episode(row)
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

    # ─── Notes CRUD ─────────────────────────────────────────

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
        db = self._ensure_db()
        if not topic or not topic.strip():
            raise ValueError("topic must not be empty")
        if not content or not content.strip():
            raise ValueError("content must not be empty")
        content_bytes = content.encode("utf-8")
        if len(content_bytes) > _MAX_NOTE_CONTENT_BYTES:
            raise ValueError(
                f"content exceeds {_MAX_NOTE_CONTENT_BYTES} byte limit "
                f"({len(content_bytes)} bytes)"
            )

        # Prune oldest low-access notes if at capacity.
        await self._prune_notes(db, max_notes)

        note_id = str(uuid.uuid4())
        now = time.time()
        await db.execute(
            """
            INSERT INTO notes
                (id, agent_id, topic, content, tags_json,
                 access_count, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                note_id,
                self._agent_id,
                topic.strip(),
                content,
                json.dumps(tags or []),
                now,
                now,
            ),
        )
        await db.commit()
        return note_id

    async def recall_notes(
        self,
        query: str = "",
        *,
        limit: int = 10,
    ) -> list[Note]:
        """Retrieve notes matching query, ranked by relevance.

        Increments access_count on returned notes.
        """
        if limit < 1:
            raise ValueError(f"limit must be >= 1, got {limit}")
        limit = min(limit, _MAX_RECALL_LIMIT)
        db = self._ensure_db()

        if query and self._fts5:
            rows = await self._recall_notes_fts5(db, query, limit)
        elif query:
            rows = await self._recall_notes_like(db, query, limit)
        else:
            rows = await self._recall_notes_recency(db, limit)

        notes = [self._row_to_note(row) for row in rows]

        if notes:
            ids = [n.id for n in notes]
            placeholders = ",".join("?" for _ in ids)
            await db.execute(
                f"UPDATE notes SET access_count = access_count + 1 "
                f"WHERE id IN ({placeholders})",
                ids,
            )
            await db.commit()
            for note in notes:
                note.access_count += 1

        return notes

    async def update_note(self, note_id: str, content: str) -> bool:
        """Update note content. Topic and tags preserved. Returns True if found."""
        db = self._ensure_db()
        if not content or not content.strip():
            raise ValueError("content must not be empty")
        content_bytes = content.encode("utf-8")
        if len(content_bytes) > _MAX_NOTE_CONTENT_BYTES:
            raise ValueError(
                f"content exceeds {_MAX_NOTE_CONTENT_BYTES} byte limit "
                f"({len(content_bytes)} bytes)"
            )
        now = time.time()
        cursor = await db.execute(
            "UPDATE notes SET content = ?, updated_at = ? "
            "WHERE id = ? AND agent_id = ?",
            (content, now, note_id, self._agent_id),
        )
        await db.commit()
        return cursor.rowcount > 0

    async def delete_note(self, note_id: str) -> bool:
        """Delete a note by ID (agent-scoped). Returns True if found."""
        db = self._ensure_db()
        cursor = await db.execute(
            "DELETE FROM notes WHERE id = ? AND agent_id = ?",
            (note_id, self._agent_id),
        )
        await db.commit()
        return cursor.rowcount > 0

    async def count_notes(self) -> int:
        """Return the number of notes for this agent."""
        db = self._ensure_db()
        async with db.execute(
            "SELECT COUNT(*) FROM notes WHERE agent_id = ?",
            (self._agent_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return row[0] if row else 0

    async def _prune_notes(self, db: aiosqlite.Connection, max_notes: int) -> None:
        """Remove oldest low-access notes when count >= max_notes.

        Uses a single atomic DELETE with a subquery to avoid a TOCTOU race
        between SELECT count and DELETE that could occur when multiple
        EpisodicMemory instances share a DB file (F-3b-1).
        """
        await db.execute(
            """
            DELETE FROM notes WHERE id IN (
                SELECT id FROM notes
                WHERE agent_id = ?
                ORDER BY access_count ASC, created_at ASC
                LIMIT MAX(0, (SELECT COUNT(*) FROM notes WHERE agent_id = ?) - ? + 1)
            )
            """,
            (self._agent_id, self._agent_id, max_notes),
        )

    async def _recall_notes_fts5(
        self,
        db: aiosqlite.Connection,
        query: str,
        limit: int,
    ) -> list[aiosqlite.Row]:
        """FTS5 search across topic, content, and tags."""
        try:
            async with db.execute(
                f"""
                SELECT {", ".join(f"n.{c}" for c in _NOTE_COLS)}
                FROM notes_fts fts
                JOIN notes n ON n.rowid = fts.rowid
                WHERE notes_fts MATCH ?
                  AND n.agent_id = ?
                ORDER BY fts.rank * -1 DESC
                LIMIT ?
                """,
                (query, self._agent_id, limit),
            ) as cursor:
                return await cursor.fetchall()
        except sqlite3.OperationalError as exc:
            logger.warning(
                "Notes FTS5 query failed for %r, falling back to LIKE: %s",
                query,
                exc,
            )
            return await self._recall_notes_like(db, query, limit)

    async def _recall_notes_like(
        self,
        db: aiosqlite.Connection,
        query: str,
        limit: int,
    ) -> list[aiosqlite.Row]:
        """LIKE fallback when FTS5 is unavailable."""
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        async with db.execute(
            f"""
            SELECT {_NOTE_SELECT}
            FROM notes
            WHERE agent_id = ?
              AND (topic LIKE ? ESCAPE '\\'
                   OR content LIKE ? ESCAPE '\\'
                   OR tags_json LIKE ? ESCAPE '\\')
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (self._agent_id, pattern, pattern, pattern, limit),
        ) as cursor:
            return await cursor.fetchall()

    async def _recall_notes_recency(
        self,
        db: aiosqlite.Connection,
        limit: int,
    ) -> list[aiosqlite.Row]:
        """No query — return most recently updated notes."""
        async with db.execute(
            f"""
            SELECT {_NOTE_SELECT}
            FROM notes
            WHERE agent_id = ?
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (self._agent_id, limit),
        ) as cursor:
            return await cursor.fetchall()

    def _row_to_note(self, row: aiosqlite.Row) -> Note:
        """Convert a database row to a Note dataclass."""
        return Note(
            id=row[0],
            agent_id=row[1],
            topic=row[2],
            content=row[3],
            tags=json.loads(row[4]) if row[4] else [],
            access_count=row[5],
            created_at=row[6],
            updated_at=row[7],
        )

    # ─── Interaction counter (auto_reflect_after) ───────────

    async def get_interaction_count(self) -> int:
        """Get the current interaction count for this agent."""
        db = self._ensure_db()
        async with db.execute(
            "SELECT interaction_count FROM agent_state WHERE agent_id = ?",
            (self._agent_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return row[0] if row else 0

    async def increment_interaction_count(self) -> int:
        """Increment and return the new interaction count.

        Creates the agent_state row if it doesn't exist (upsert).
        Uses RETURNING to get the post-upsert count in a single round-trip,
        eliminating a read-after-write race (F-3b-2).  Requires SQLite >= 3.35
        (Python 3.11+ ships >= 3.39).
        """
        db = self._ensure_db()
        now = time.time()
        cursor = await db.execute(
            """
            INSERT INTO agent_state (agent_id, interaction_count, updated_at)
            VALUES (?, 1, ?)
            ON CONFLICT(agent_id) DO UPDATE
                SET interaction_count = interaction_count + 1,
                    updated_at = ?
            RETURNING interaction_count
            """,
            (self._agent_id, now, now),
        )
        row = await cursor.fetchone()
        await db.commit()
        return row[0] if row else 0

    async def reset_interaction_count(self) -> None:
        """Reset the interaction counter to zero."""
        db = self._ensure_db()
        now = time.time()
        await db.execute(
            """
            INSERT INTO agent_state (agent_id, interaction_count, updated_at)
            VALUES (?, 0, ?)
            ON CONFLICT(agent_id) DO UPDATE
                SET interaction_count = 0,
                    updated_at = ?
            """,
            (self._agent_id, now, now),
        )
        await db.commit()

    # ─── Persona state persistence ──────────────────────────

    async def persist_agent_state(
        self, agent_id: str, state_json: str,
    ) -> None:
        """Persist opaque agent state JSON to the agent_state table.

        Uses INSERT … ON CONFLICT to upsert only the persona_state_json
        and updated_at columns, preserving interaction_count managed by
        the interaction counter methods above.
        """
        db = self._ensure_db()
        now = time.time()
        await db.execute(
            """
            INSERT INTO agent_state
                (agent_id, interaction_count, persona_state_json, updated_at)
            VALUES (?, 0, ?, ?)
            ON CONFLICT(agent_id) DO UPDATE
                SET persona_state_json = ?,
                    updated_at = ?
            """,
            (agent_id, state_json, now, state_json, now),
        )
        await db.commit()

    async def load_agent_state(self, agent_id: str) -> str | None:
        """Load opaque agent state JSON from the agent_state table.

        Returns ``None`` if no state has been persisted for this agent.
        """
        db = self._ensure_db()
        async with db.execute(
            "SELECT persona_state_json FROM agent_state WHERE agent_id = ?",
            (agent_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row and row[0]:
            return row[0]
        return None
