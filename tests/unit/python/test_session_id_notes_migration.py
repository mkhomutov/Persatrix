"""
Tests for RFC 0031 Phase 2 PR 1 migration v9 — session_id on notes.

Phase 1 (migration v7) added ``session_id`` to ``episodes`` and
``relationships``; RFC 0026's migration v8 added it to ``facts``.  The
``notes`` tier (migration v2) was the remaining persona-memory recall
surface with no session dimension, which would re-introduce F-3 on the
notes prompt surface once the other tiers are filtered.  Migration v9
closes that gap with the column + per-table index, mirroring v7's shape.

Covers:

* fresh-DB initialisation runs migration v9 and the new ``session_id``
  column + ``idx_notes_session`` index exist on ``notes``.
* in-place upgrade from a v8 baseline picks up the new column with the
  synthetic ``"legacy"`` default for every pre-existing note row.
* migration is idempotent — re-running ``_apply_migrations`` is a no-op
  and the column existence guard prevents double-ALTER.
* the no-notes baseline (partial-restore shape) is a clean no-op for the
  handler — same contract as v5/v6/v7.
* the umbrella records v9 even when the handler branch short-circuits.

Mirrors :mod:`tests.unit.python.test_session_id_migration` (the v7 pin
file) so a future refactor that drops the no-op guard from one but not the
other is caught.
"""

from __future__ import annotations

import aiosqlite
import pytest

from agents.memory.episodic import EpisodicMemory
from agents.memory.migrations import (
    MIGRATIONS,
    _apply_migration_9,
    _apply_migrations,
)


async def _columns(db: aiosqlite.Connection, table: str) -> set[str]:
    cursor = await db.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cursor.fetchall()}


async def _index_exists(db: aiosqlite.Connection, name: str) -> bool:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
        (name,),
    )
    return await cursor.fetchone() is not None


# ─── Fresh-DB migration ─────────────────────────────────────


class TestFreshSchemaMigration:
    async def test_migration_9_registered(self):
        versions = [v for v, _, _ in MIGRATIONS]
        assert 9 in versions

    async def test_notes_session_column_present(self, memory: EpisodicMemory):
        cols = await _columns(memory._ensure_db(), "notes")
        assert "session_id" in cols

    async def test_notes_session_index_created(self, memory: EpisodicMemory):
        assert await _index_exists(memory._ensure_db(), "idx_notes_session")

    async def test_schema_version_records_v9(self, memory: EpisodicMemory):
        async with memory._ensure_db().execute(
            "SELECT MAX(version) FROM schema_version",
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] >= 9


# ─── Idempotency ────────────────────────────────────────────


class TestMigrationIdempotency:
    async def test_double_apply_is_noop(self, memory: EpisodicMemory):
        db = memory._ensure_db()
        await _apply_migrations(db)
        await _apply_migrations(db)
        async with db.execute(
            "SELECT COUNT(*) FROM schema_version WHERE version = 9",
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] == 1

    async def test_direct_handler_replay_is_safe(self, memory: EpisodicMemory):
        # Simulate the crash-between-DDL-and-version-record case.  Calling
        # the handler again on an already-migrated DB must not raise and
        # must not produce duplicate columns.
        db = memory._ensure_db()
        await _apply_migration_9(db)
        cols = await _columns(db, "notes")
        assert sum(1 for c in cols if c == "session_id") == 1
        assert await _index_exists(db, "idx_notes_session")


# ─── Empty-baseline guard ───────────────────────────────────


class TestEmptyTableGuard:
    """v9's handler must be a no-op when the notes table is missing.

    Mirrors the v5 / v6 / v7 contract: a partial-restore shape with
    ``schema_version`` recorded up to v8 but no ``notes`` table.
    ``ALTER TABLE`` on a nonexistent table would raise; the handler
    detects the missing table via ``sqlite_master`` and skips cleanly.
    """

    async def test_handler_no_op_on_missing_notes(self):
        db = await aiosqlite.connect(":memory:")
        try:
            await db.execute(
                "CREATE TABLE schema_version "
                "(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL, "
                "description TEXT)",
            )
            await db.execute(
                "INSERT INTO schema_version VALUES (8, 0.0, 'baseline')",
            )
            await db.commit()

            await _apply_migration_9(db)

            # Outer harness owns the version record — a direct handler
            # call must not touch ``schema_version``.
            cursor = await db.execute(
                "SELECT COUNT(*) FROM schema_version WHERE version = 9",
            )
            row = await cursor.fetchone()
            assert row[0] == 0

            # No notes table was present, so nothing was created.
            cursor = await db.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='notes'",
            )
            assert await cursor.fetchone() is None
        finally:
            await db.close()

    async def test_umbrella_records_v9_even_on_full_no_op(self):
        # Even when the notes table is missing, the umbrella records v9 as
        # applied — same contract as v5/v6/v7 so a later baseline rerun
        # cannot loop the upgrade.
        db = await aiosqlite.connect(":memory:")
        try:
            await db.execute(
                "CREATE TABLE schema_version "
                "(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL, "
                "description TEXT)",
            )
            await db.execute(
                "INSERT INTO schema_version VALUES (8, 0.0, 'v8 baseline')",
            )
            await db.commit()

            await _apply_migrations(db)

            cursor = await db.execute(
                "SELECT COUNT(*) FROM schema_version WHERE version = 9",
            )
            row = await cursor.fetchone()
            assert row[0] == 1
        finally:
            await db.close()


# ─── Legacy upgrade path ────────────────────────────────────


class TestLegacyUpgrade:
    async def test_upgrade_from_v8_backfills_legacy(self):
        """A DB pinned at v8 picks up v9 with ``'legacy'`` on existing rows."""
        db = await aiosqlite.connect(":memory:")
        try:
            await db.execute(
                "CREATE TABLE schema_version "
                "(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL, "
                "description TEXT)",
            )
            import time as _time

            from agents.memory.migrations import _MIGRATION_HANDLERS

            for version, desc, sql in MIGRATIONS:
                if version > 8:
                    continue
                handler = _MIGRATION_HANDLERS.get(version)
                if handler is not None:
                    await handler(db)
                else:
                    await db.executescript(sql)
                await db.execute(
                    "INSERT INTO schema_version VALUES (?, ?, ?)",
                    (version, _time.time(), desc),
                )
            await db.commit()

            # Insert a legacy note row shaped at the v8 schema (no
            # session_id column yet — migration v2 columns only).
            await db.execute(
                """
                INSERT INTO notes
                    (id, agent_id, topic, content, tags_json,
                     access_count, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    "legacy-note-1", "agent-x", "pre-session topic",
                    "pre-session content", "[]", 1000.0, 1000.0,
                ),
            )
            await db.commit()

            # Run the umbrella migration runner — picks up v9.
            await _apply_migrations(db)

            assert "session_id" in await _columns(db, "notes")

            async with db.execute(
                "SELECT session_id FROM notes WHERE id = ?",
                ("legacy-note-1",),
            ) as cursor:
                row = await cursor.fetchone()
            assert row[0] == "legacy"
        finally:
            await db.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
