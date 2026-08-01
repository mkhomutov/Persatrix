"""
Schema migrations and shared scoring SQL fragments.

Forward-only migrations applied by ``_apply_migrations()`` and shared
scoring constants used by ``episodic.py``.

The ``MIGRATIONS`` registry itself lives in
:mod:`agents.memory._migration_registry` (re-exported here): it is reference
data whose length grows with every schema version, so keeping it out leaves
this module's *logic* honestly under the 500-line cap.

The callable migration handlers (currently ``v4`` through ``v12``) live
in :mod:`agents.memory._migration_handlers` — itself split across that
module, :mod:`agents.memory._migration_facts` (v8),
:mod:`agents.memory._migration_notes_session` (v9),
:mod:`agents.memory._migration_interactions_session` (v10),
:mod:`agents.memory._migration_principal` (v11), and
:mod:`agents.memory._migration_epoch` (v12) to stay under the 500-line
soft cap.  All handlers are re-exported below for backwards
compatibility, so call sites and tests should keep importing them from
this module.
"""

from __future__ import annotations

import logging
import time

import aiosqlite

# Re-export the callable migration handlers + registry from the helper
# module.  Pulled out to keep this file under the 500-line repo cap.
from ._migration_handlers import (
    _MIGRATION_HANDLERS,
    _apply_migration_4,
    _apply_migration_5,
    _apply_migration_6,
    _apply_migration_7,
    _apply_migration_8,
    _apply_migration_9,
    _apply_migration_10,
    _apply_migration_11,
    _apply_migration_12,
    _apply_migration_13,
    _apply_migration_14,
    _apply_migration_15,
    _apply_migration_16,
    _apply_migration_17,
)
from ._migration_protection import PROTECTION_LEVEL_DEFAULT
from ._migration_registry import MIGRATIONS

__all__ = [
    "MIGRATIONS",
    "PROTECTION_LEVEL_DEFAULT",
    "_FTS5_DDL",
    "_MIGRATION_HANDLERS",
    "_NOTES_FTS5_DDL",
    "_SCORE_EXPR",
    "_SCORE_EXPR_BARE",
    "_apply_migration_4",
    "_apply_migration_5",
    "_apply_migration_6",
    "_apply_migration_7",
    "_apply_migration_8",
    "_apply_migration_9",
    "_apply_migration_10",
    "_apply_migration_11",
    "_apply_migration_12",
    "_apply_migration_13",
    "_apply_migration_14",
    "_apply_migration_15",
    "_apply_migration_16",
    "_apply_migration_17",
    "_apply_migrations",
    "_fts5_available",
]

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

# ``MIGRATIONS`` — the forward-only registry — is imported above from
# :mod:`agents.memory._migration_registry` and re-exported here, so every
# existing ``from .migrations import MIGRATIONS`` call site is unchanged.
# It is reference data whose length scales with migration history rather than
# with authored logic, kept out of this module for the same "size scales with
# data, not prose" reason as ``_MIGRATION_HANDLERS`` above.



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

            # Check for a callable migration handler (e.g. _apply_migration_4).
            # Uses the explicit _MIGRATION_HANDLERS registry instead of
            # globals().get() for IDE discoverability and refactoring safety.
            # (PR 6 review fix: PR 2 finding #1.)
            handler = _MIGRATION_HANDLERS.get(version)
            if handler is not None:
                await handler(db)
            elif sql:
                await db.executescript(sql)
            else:
                # No callable handler found and SQL is empty — this is a
                # programming error (e.g. migration version not registered
                # in _MIGRATION_HANDLERS).
                # Without this guard, the migration would silently be
                # recorded as applied without actually running.
                # (PR #120 review F-4: globals().get() dispatch fragility.)
                raise RuntimeError(
                    f"Migration v{version} has no SQL and no callable "
                    f"handler in _MIGRATION_HANDLERS"
                )
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
