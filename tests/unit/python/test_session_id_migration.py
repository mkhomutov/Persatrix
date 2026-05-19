"""
Tests for RFC 0031 Phase 1 migration v7 — session_id on episodes/relationships.

Covers:

* fresh-DB initialisation runs migration v7 and the new ``session_id``
  columns + per-table indexes exist on ``episodes`` and ``relationships``.
* in-place upgrade from a v6 baseline picks up the new column with the
  synthetic ``"legacy"`` default for every pre-existing row.
* migration is idempotent — re-running ``_apply_migrations`` is a no-op
  and the column existence guard prevents double-ALTER.
* the no-episodes / no-relationships baseline (partial-restore shape) is
  a clean no-op for the handler — same contract as v5/v6.
* the umbrella records v7 even when both handler branches short-circuit.

Mirrors :mod:`tests.unit.python.test_episodic_schema_migration` (the RFC
0020 §D v5 migration's pin file) — same handler-level + umbrella-level
shape so a future refactor that drops the no-op guard from one but not the
other is caught by the parametrised iteration.
"""

from __future__ import annotations

import aiosqlite
import pytest

from agents.memory.episodic import EpisodicMemory
from agents.memory.migrations import (
    MIGRATIONS,
    _apply_migration_7,
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
    async def test_migration_7_registered(self):
        versions = [v for v, _, _ in MIGRATIONS]
        assert 7 in versions

    async def test_episodes_session_column_present(
        self, memory: EpisodicMemory,
    ):
        cols = await _columns(memory._ensure_db(), "episodes")
        assert "session_id" in cols

    async def test_relationships_session_column_present(
        self, memory: EpisodicMemory,
    ):
        # The relationships table is created by migration v4; v7 adds the
        # session_id column.  We use the EpisodicMemory fixture because it
        # shares the database and migration runner with RelationshipMemory
        # — both modules call ``_apply_migrations`` against the same file.
        cols = await _columns(memory._ensure_db(), "relationships")
        assert "session_id" in cols

    async def test_episodes_session_index_created(self, memory: EpisodicMemory):
        assert await _index_exists(memory._ensure_db(), "idx_episodes_session")

    async def test_relationships_session_index_created(
        self, memory: EpisodicMemory,
    ):
        assert await _index_exists(memory._ensure_db(), "idx_rel_session")

    async def test_schema_version_records_v7(self, memory: EpisodicMemory):
        async with memory._ensure_db().execute(
            "SELECT MAX(version) FROM schema_version",
        ) as cursor:
            row = await cursor.fetchone()
        assert row[0] >= 7


# ─── Idempotency ────────────────────────────────────────────


class TestMigrationIdempotency:
    async def test_double_apply_is_noop(self, memory: EpisodicMemory):
        db = memory._ensure_db()
        await _apply_migrations(db)
        await _apply_migrations(db)
        async with db.execute(
            "SELECT COUNT(*) FROM schema_version WHERE version = 7",
        ) as cursor:
            row = await cursor.fetchone()
        assert row[0] == 1

    async def test_direct_handler_replay_is_safe(self, memory: EpisodicMemory):
        # Simulate the crash-between-DDL-and-version-record case.  Calling
        # the handler again on an already-migrated DB must not raise and
        # must not produce duplicate columns.
        db = memory._ensure_db()
        await _apply_migration_7(db)
        ep_cols = await _columns(db, "episodes")
        rel_cols = await _columns(db, "relationships")
        assert sum(1 for c in ep_cols if c == "session_id") == 1
        assert sum(1 for c in rel_cols if c == "session_id") == 1


# ─── Empty-baseline guard ───────────────────────────────────


class TestEmptyTableGuards:
    """v7's handler must be a no-op when either table is missing.

    Mirrors the v5 / v6 contract (PR-1 review finding #4 precedent): a
    partial-restore shape with ``schema_version`` recorded up to v6 but
    no ``episodes`` and/or ``relationships`` table.  ``ALTER TABLE`` on a
    nonexistent table would raise; the handler instead detects the
    missing table via ``sqlite_master`` and skips that half cleanly.

    The two-table shape means we parametrise across both missing-table
    permutations + the both-missing baseline.
    """

    @pytest.mark.parametrize(
        "create_episodes, create_relationships",
        [
            (False, False),
            (True, False),
            (False, True),
        ],
        ids=["both-missing", "only-episodes", "only-relationships"],
    )
    async def test_handler_no_op_on_missing_tables(
        self, create_episodes, create_relationships,
    ):
        db = await aiosqlite.connect(":memory:")
        try:
            await db.execute(
                "CREATE TABLE schema_version "
                "(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL, "
                "description TEXT)",
            )
            await db.execute(
                "INSERT INTO schema_version VALUES (6, 0.0, 'baseline')",
            )
            if create_episodes:
                await db.execute(
                    "CREATE TABLE episodes (id TEXT PRIMARY KEY)",
                )
            if create_relationships:
                await db.execute(
                    "CREATE TABLE relationships ("
                    "participant_id TEXT, other_participant_id TEXT, "
                    "PRIMARY KEY (participant_id, other_participant_id))",
                )
            await db.commit()

            await _apply_migration_7(db)

            # Outer harness owns the version record — a direct handler
            # call must not touch ``schema_version``.
            cursor = await db.execute(
                "SELECT COUNT(*) FROM schema_version WHERE version = 7",
            )
            row = await cursor.fetchone()
            assert row[0] == 0

            # If a stub table was present, the column was added.  If it
            # was absent, nothing was created.
            if create_episodes:
                assert "session_id" in await _columns(db, "episodes")
            else:
                cursor = await db.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name='episodes'",
                )
                assert await cursor.fetchone() is None

            if create_relationships:
                assert "session_id" in await _columns(db, "relationships")
            else:
                cursor = await db.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name='relationships'",
                )
                assert await cursor.fetchone() is None
        finally:
            await db.close()

    async def test_umbrella_records_v7_even_on_full_no_op(self):
        # Even when both tables are missing, the umbrella records v7 as
        # applied — same contract as v5/v6 so a later baseline rerun
        # cannot loop the upgrade.
        db = await aiosqlite.connect(":memory:")
        try:
            await db.execute(
                "CREATE TABLE schema_version "
                "(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL, "
                "description TEXT)",
            )
            await db.execute(
                "INSERT INTO schema_version VALUES (6, 0.0, 'v6 baseline')",
            )
            await db.commit()

            await _apply_migrations(db)

            cursor = await db.execute(
                "SELECT COUNT(*) FROM schema_version WHERE version = 7",
            )
            row = await cursor.fetchone()
            assert row[0] == 1
        finally:
            await db.close()


# ─── Legacy upgrade path ────────────────────────────────────


class TestLegacyUpgrade:
    async def test_upgrade_from_v6_backfills_legacy(self):
        """A DB pinned at v6 picks up v7 with ``'legacy'`` on existing rows."""
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
                if version > 6:
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

            # Insert legacy rows shaped at the v6 schema.
            await db.execute(
                """
                INSERT INTO episodes
                    (id, agent_id, summary, context_json, outcome,
                     importance, access_count, last_accessed_at,
                     tags_json, created_at, compressed_at, compression_level)
                VALUES (?, ?, ?, ?, ?, ?, 0, NULL, ?, ?, NULL, 0)
                """,
                (
                    "legacy-ep-1", "agent-x", "pre-session summary", "{}",
                    None, 0.5, "[]", 1000.0,
                ),
            )
            await db.execute(
                """
                INSERT INTO relationships
                    (participant_id, participant_type,
                     other_participant_id, other_participant_type,
                     trust_score, interaction_count,
                     last_interaction_at, notes)
                VALUES ('agent-x', 'agent', 'agent-y', 'agent',
                        0.6, 3, 1234.0, NULL)
                """,
            )
            await db.commit()

            # Run the umbrella migration runner — picks up v7.
            await _apply_migrations(db)

            assert "session_id" in await _columns(db, "episodes")
            assert "session_id" in await _columns(db, "relationships")

            async with db.execute(
                "SELECT session_id FROM episodes WHERE id = ?",
                ("legacy-ep-1",),
            ) as cursor:
                row = await cursor.fetchone()
            assert row[0] == "legacy"

            async with db.execute(
                "SELECT session_id FROM relationships "
                "WHERE participant_id = ? AND other_participant_id = ?",
                ("agent-x", "agent-y"),
            ) as cursor:
                row = await cursor.fetchone()
            assert row[0] == "legacy"
        finally:
            await db.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
