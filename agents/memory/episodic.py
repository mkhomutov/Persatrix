"""
Episodic memory — long-term storage of past interactions.

Stores summaries of conversations, decisions, and outcomes in SQLite
with FTS5 full-text search for relevance-ranked retrieval.
"""

import json
import logging
import time
import uuid
from dataclasses import dataclass
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
        """FTS5 search with composite BM25 x importance x access x recency scoring."""
        async with db.execute(
            """
            SELECT e.*
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

    async def _recall_like(
        self,
        db: aiosqlite.Connection,
        query: str,
        limit: int,
        min_importance: float,
    ) -> list[aiosqlite.Row]:
        """LIKE fallback when FTS5 is unavailable."""
        pattern = f"%{query}%"
        async with db.execute(
            """
            SELECT *
            FROM episodes
            WHERE agent_id = ?
              AND importance >= ?
              AND (summary LIKE ? OR context_json LIKE ?)
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
            """
            SELECT *
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
            "SELECT * FROM episodes WHERE id = ? AND agent_id = ?",
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
