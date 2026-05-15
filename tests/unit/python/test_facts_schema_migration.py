"""
Tests for RFC 0026 PR 1 migration v8 — ``facts`` table + indexes.

Covers:

* fresh-DB initialisation runs migration v8 and the new ``facts`` table +
  ``idx_facts_subject_agent`` + ``idx_facts_session`` indexes exist
* migration is idempotent — re-running ``_apply_migrations`` is a no-op
  and the table-existence guard prevents a re-CREATE
* in-place upgrade from a v7 baseline picks up the new table without
  touching pre-existing ``episodes`` / ``relationships`` rows
* the umbrella records v8 even when the handler short-circuits

Mirrors :mod:`tests.unit.python.test_session_id_migration` (the RFC 0031
Phase 1 v7 pin file) — same fresh + idempotency + upgrade shape so a
future refactor that drops the no-op guard from one but not the other is
caught at the parametrised iteration.
"""

from __future__ import annotations

import aiosqlite
import pytest

from agents.memory.episodic import EpisodicMemory
from agents.memory.migrations import (
    MIGRATIONS,
    _apply_migration_8,
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


async def _table_exists(db: aiosqlite.Connection, name: str) -> bool:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    )
    return await cursor.fetchone() is not None


# ─── Fresh-DB migration ─────────────────────────────────────


class TestFreshSchemaMigration:
    async def test_migration_8_registered(self):
        versions = [v for v, _, _ in MIGRATIONS]
        assert 8 in versions

    async def test_facts_table_present(self, memory: EpisodicMemory):
        assert await _table_exists(memory._ensure_db(), "facts")

    async def test_facts_table_columns(self, memory: EpisodicMemory):
        cols = await _columns(memory._ensure_db(), "facts")
        expected = {
            "fact_id",
            "agent_id",
            "subject",
            "predicate",
            "object",
            "certainty",
            "source_interaction_id",
            "asserted_at",
            "last_recalled_at",
            "superseded_by",
            "session_id",
        }
        assert expected.issubset(cols), f"missing: {expected - cols}"

    async def test_subject_agent_index_created(self, memory: EpisodicMemory):
        assert await _index_exists(
            memory._ensure_db(), "idx_facts_subject_agent",
        )

    async def test_session_index_created(self, memory: EpisodicMemory):
        assert await _index_exists(memory._ensure_db(), "idx_facts_session")

    async def test_schema_version_records_v8(self, memory: EpisodicMemory):
        async with memory._ensure_db().execute(
            "SELECT MAX(version) FROM schema_version",
        ) as cursor:
            row = await cursor.fetchone()
        assert row[0] >= 8

    async def test_session_id_default_is_legacy(
        self, memory: EpisodicMemory,
    ):
        # Insert a row omitting session_id — DEFAULT 'legacy' must apply
        # so pre-RFC-0031 inserters still produce queryable rows (mirrors
        # the migration-v7 contract on episodes / relationships).
        db = memory._ensure_db()
        await db.execute(
            """
            INSERT INTO facts
                (fact_id, agent_id, subject, predicate, object,
                 certainty, source_interaction_id, asserted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("f1", "agent-x", "bob", "has_name", "Bob",
             1.0, "ix-1", 1000.0),
        )
        await db.commit()
        async with db.execute(
            "SELECT session_id FROM facts WHERE fact_id = ?", ("f1",),
        ) as cursor:
            row = await cursor.fetchone()
        assert row[0] == "legacy"


# ─── Idempotency ────────────────────────────────────────────


class TestMigrationIdempotency:
    async def test_double_apply_is_noop(self, memory: EpisodicMemory):
        db = memory._ensure_db()
        await _apply_migrations(db)
        await _apply_migrations(db)
        async with db.execute(
            "SELECT COUNT(*) FROM schema_version WHERE version = 8",
        ) as cursor:
            row = await cursor.fetchone()
        assert row[0] == 1

    async def test_direct_handler_replay_is_safe(
        self, memory: EpisodicMemory,
    ):
        # Simulate the crash-between-DDL-and-version-record case.
        # Calling the handler on an already-migrated DB must not raise
        # and must not duplicate the table or indexes.
        db = memory._ensure_db()
        await _apply_migration_8(db)
        assert await _table_exists(db, "facts")
        assert await _index_exists(db, "idx_facts_subject_agent")
        assert await _index_exists(db, "idx_facts_session")


# ─── Pre-existing table guard ───────────────────────────────


class TestPreExistingFactsTable:
    """v8's handler must be a no-op when ``facts`` already exists.

    The RFC 0020 PR 6 finding #4 precedent (mirrored in v5/v6/v7): the
    handler may run on a partial-restore baseline where ``schema_version``
    records up to v7 but a previous v8 attempt already created the
    ``facts`` table.  ``CREATE TABLE`` (no ``IF NOT EXISTS``) would
    raise; the handler detects the table via ``sqlite_master`` and
    skips the CREATE cleanly.  Indexes ship with ``IF NOT EXISTS`` so
    they are belt-and-suspenders even on the no-op path.
    """

    async def test_handler_no_op_when_facts_present(self):
        db = await aiosqlite.connect(":memory:")
        try:
            await db.execute(
                "CREATE TABLE schema_version "
                "(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL, "
                "description TEXT)",
            )
            await db.execute(
                "INSERT INTO schema_version VALUES (7, 0.0, 'baseline')",
            )
            # Pre-create a stub facts table to simulate a partial replay.
            await db.execute(
                "CREATE TABLE facts (fact_id TEXT PRIMARY KEY)",
            )
            await db.commit()

            await _apply_migration_8(db)

            # Stub table preserved exactly — handler did not DROP / re-CREATE
            # (which would have lost the pre-existing rows on a real DB).
            cols = await _columns(db, "facts")
            assert cols == {"fact_id"}, (
                f"v8 handler should not overwrite an existing facts table; "
                f"saw cols={cols}"
            )

            # Outer harness owns the version record — a direct handler
            # call must not touch schema_version.
            cursor = await db.execute(
                "SELECT COUNT(*) FROM schema_version WHERE version = 8",
            )
            row = await cursor.fetchone()
            assert row[0] == 0
        finally:
            await db.close()

    async def test_umbrella_records_v8_even_on_no_op(self):
        # Even when facts is pre-created, the umbrella records v8 — same
        # contract as v5/v6/v7 so a later baseline rerun cannot loop the
        # upgrade.
        db = await aiosqlite.connect(":memory:")
        try:
            await db.execute(
                "CREATE TABLE schema_version "
                "(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL, "
                "description TEXT)",
            )
            await db.execute(
                "INSERT INTO schema_version VALUES (7, 0.0, 'v7 baseline')",
            )
            await db.execute(
                "CREATE TABLE facts (fact_id TEXT PRIMARY KEY)",
            )
            await db.commit()

            await _apply_migrations(db)

            cursor = await db.execute(
                "SELECT COUNT(*) FROM schema_version WHERE version = 8",
            )
            row = await cursor.fetchone()
            assert row[0] == 1
        finally:
            await db.close()


# ─── Legacy upgrade path ────────────────────────────────────


class TestLegacyUpgrade:
    async def test_upgrade_from_v7_preserves_existing_rows(self):
        """A DB pinned at v7 picks up v8 without touching episodes / relationships."""
        db = await aiosqlite.connect(":memory:")
        try:
            await db.execute(
                "CREATE TABLE schema_version "
                "(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL, "
                "description TEXT)",
            )
            from agents.memory.migrations import _MIGRATION_HANDLERS

            import time as _time

            for version, desc, sql in MIGRATIONS:
                if version > 7:
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

            # Insert a v7-shaped episode row (carries session_id).
            await db.execute(
                """
                INSERT INTO episodes
                    (id, agent_id, summary, context_json, outcome,
                     importance, access_count, last_accessed_at,
                     tags_json, created_at, compressed_at, compression_level,
                     session_id)
                VALUES (?, ?, ?, ?, ?, ?, 0, NULL, ?, ?, NULL, 0, ?)
                """,
                (
                    "ep-pre-v8", "agent-x", "v7 summary", "{}", None,
                    0.5, "[]", 1000.0, "run-a",
                ),
            )
            await db.commit()

            # Run the umbrella migration runner — picks up v8.
            await _apply_migrations(db)

            assert await _table_exists(db, "facts")

            # Pre-v8 episode row untouched.
            async with db.execute(
                "SELECT summary, session_id FROM episodes WHERE id = ?",
                ("ep-pre-v8",),
            ) as cursor:
                row = await cursor.fetchone()
            assert row == ("v7 summary", "run-a")
        finally:
            await db.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
