"""
Schema migrations and shared scoring SQL fragments.

Forward-only migrations applied by ``_apply_migrations()`` and shared
scoring constants used by ``episodic.py``.
"""

from __future__ import annotations

import logging
import time

import aiosqlite

logger = logging.getLogger(__name__)


# ─── Shared scoring SQL fragments ──────────────────────────

# Non-BM25 scoring components shared across _recall_fts5(), _recall_like(),
# and _recall_recency().  Extracted to avoid maintaining the same formula in
# three SQL strings (F-3a-2).
#
# importance is wrapped with (0.1 + importance * 0.9) so that episodes with
# importance=0.0 still receive a non-zero score (10% baseline) instead of
# being invisible in ranked recall (F-3a-1).
#
# A single template parameterizes the column prefix ("e." for JOINed queries,
# "" for bare queries) so formula tuning is a single edit (F-59-2).
#
# Security: {p} is ONLY filled with hardcoded column prefixes ("e." or "").
# NEVER pass user or LLM input into _SCORE_TEMPLATE.format().
# All dynamic values use parameterized ? placeholders.
_SCORE_TEMPLATE = (
    "(0.1 + {p}importance * 0.9)"
    " * (1.0 + ln(1 + {p}access_count))"
    " * (1.0 / (1 + (? - {p}created_at) / 86400.0))"  # ? = time.time()
)
_SCORE_EXPR = _SCORE_TEMPLATE.format(p="e.")
_SCORE_EXPR_BARE = _SCORE_TEMPLATE.format(p="")


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
