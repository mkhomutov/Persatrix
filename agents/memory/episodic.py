"""
Episodic memory — long-term storage of past interactions.

Stores summaries of conversations, decisions, and outcomes in SQLite
with FTS5 full-text search for relevance-ranked retrieval.

Also provides agent-initiated note storage (migration v2) for
structured knowledge the agent chooses to persist.
"""

import json
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import aiosqlite

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
                    * e.importance
                    * (1.0 + ln(1 + e.access_count))
                    * (1.0 / (1 + (? - e.created_at) / 86400.0))
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
                importance
                * (1.0 + ln(1 + access_count))
                * (1.0 / (1 + (? - created_at) / 86400.0))
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
                importance
                * (1.0 + ln(1 + access_count))
                * (1.0 / (1 + (? - created_at) / 86400.0))
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
        """Remove oldest low-access notes when count >= max_notes."""
        async with db.execute(
            "SELECT COUNT(*) FROM notes WHERE agent_id = ?",
            (self._agent_id,),
        ) as cursor:
            row = await cursor.fetchone()
        count = row[0] if row else 0
        if count < max_notes:
            return
        # Delete the oldest, least-accessed note(s) to make room.
        overflow = count - max_notes + 1
        await db.execute(
            """
            DELETE FROM notes WHERE id IN (
                SELECT id FROM notes
                WHERE agent_id = ?
                ORDER BY access_count ASC, created_at ASC
                LIMIT ?
            )
            """,
            (self._agent_id, overflow),
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
        except sqlite3.OperationalError:
            logger.warning(
                "Notes FTS5 query failed for %r, falling back to LIKE", query,
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
        """
        db = self._ensure_db()
        now = time.time()
        await db.execute(
            """
            INSERT INTO agent_state (agent_id, interaction_count, updated_at)
            VALUES (?, 1, ?)
            ON CONFLICT(agent_id) DO UPDATE
                SET interaction_count = interaction_count + 1,
                    updated_at = ?
            """,
            (self._agent_id, now, now),
        )
        await db.commit()
        async with db.execute(
            "SELECT interaction_count FROM agent_state WHERE agent_id = ?",
            (self._agent_id,),
        ) as cursor:
            row = await cursor.fetchone()
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
