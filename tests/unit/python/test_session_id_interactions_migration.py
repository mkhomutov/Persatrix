"""
Tests for RFC 0031 Phase 2 PR 5 migration v10 — session_id on interactions.

Migrations v7 / v8 / v9 added ``session_id`` to ``episodes`` /
``relationships`` / ``facts`` / ``notes``; v10 closes the last
persona-memory recall surface that fed the prompt without a session
dimension — the ``interactions`` table fetched by
:func:`agents.memory.relationship_queries.get_relationship_summary`'s
secondary SELECT (`ISSUE-0080
<../../../docs/issues/ISSUE-0080-relationship-recent-interactions-cross-session-leak.md>`_).

Covers:

* fresh-DB initialisation runs migration v10 and the ``session_id``
  column + ``idx_interactions_session`` index exist on ``interactions``.
* in-place upgrade from a v9 baseline picks up the new column with the
  synthetic ``"legacy"`` default for every pre-existing interaction row.
* umbrella replay (schema_version v10 row deleted to simulate the
  crash-between-DDL-and-version-record case) is idempotent.
* direct-handler replay is safe — invoking ``_apply_migration_10``
  twice from a v9 baseline produces no duplicate column / index.
* the no-interactions baseline (partial-restore shape) is a clean
  no-op for the handler — same contract as v7/v8/v9.

Mirrors :mod:`tests.unit.python.test_session_id_notes_migration` (the
v9 pin file) so a future refactor that drops the no-op guard from one
but not the other is caught.
"""

from __future__ import annotations

import time as _time

import aiosqlite
import pytest

from agents.memory.episodic import EpisodicMemory
from agents.memory.migrations import (
    _MIGRATION_HANDLERS,
    MIGRATIONS,
    _apply_migration_10,
    _apply_migrations,
)


async def _columns(db: aiosqlite.Connection, table: str) -> set[str]:
    cursor = await db.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cursor.fetchall()}


async def _column_count(
    db: aiosqlite.Connection, table: str, name: str,
) -> int:
    """Count raw PRAGMA rows so a duplicate ``ADD COLUMN`` is witnessed."""
    cursor = await db.execute(f"PRAGMA table_info({table})")
    return sum(1 for row in await cursor.fetchall() if row[1] == name)


async def _index_exists(db: aiosqlite.Connection, name: str) -> bool:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
        (name,),
    )
    return await cursor.fetchone() is not None


async def _seed_v9_baseline(db: aiosqlite.Connection) -> None:
    """Walk MIGRATIONS up to and including v9, recording each in
    ``schema_version``.  Leaves the DB at the schema state immediately
    before v10.
    """
    await db.execute(
        "CREATE TABLE IF NOT EXISTS schema_version "
        "(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL, "
        "description TEXT)",
    )
    for version, desc, sql in MIGRATIONS:
        if version > 9:
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


# ─── Fresh-DB migration ─────────────────────────────────────


class TestFreshSchemaMigration:
    async def test_migration_10_registered(self):
        versions = [v for v, _, _ in MIGRATIONS]
        assert 10 in versions

    @pytest.fixture
    async def memory(self):
        mem = EpisodicMemory(agent_id="t", db_path=":memory:")
        await mem.initialize()
        # Initialize relationships tier too so the ``interactions``
        # table exists (it ships with migration v3).
        yield mem
        await mem.close()

    async def test_interactions_session_column_present(
        self, memory: EpisodicMemory,
    ) -> None:
        cols = await _columns(memory._db, "interactions")
        assert "session_id" in cols

    async def test_interactions_session_index_created(
        self, memory: EpisodicMemory,
    ) -> None:
        assert await _index_exists(memory._db, "idx_interactions_session")

    async def test_schema_version_records_v10(
        self, memory: EpisodicMemory,
    ) -> None:
        async with memory._db.execute(
            "SELECT COUNT(*) FROM schema_version WHERE version = 10",
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] == 1


# ─── Idempotency ────────────────────────────────────────────


class TestMigrationIdempotency:
    async def test_umbrella_replay_after_version_record_loss_is_noop(self):
        """Umbrella replay must be idempotent after a crash between the v10
        DDL and the ``schema_version`` INSERT.
        """
        db = await aiosqlite.connect(":memory:")
        try:
            await _seed_v9_baseline(db)

            # First umbrella pass — brings DB to v10, records the row.
            await _apply_migrations(db)
            assert await _column_count(db, "interactions", "session_id") == 1
            assert await _index_exists(db, "idx_interactions_session")

            # Simulate crash-between-DDL-and-version-record: drop the
            # v10 row from schema_version while leaving the column +
            # index in place.  Drop any later versions too so the
            # umbrella's ``current = MAX(version)`` reads as 9 and
            # re-dispatches the v10 handler.
            await db.execute("DELETE FROM schema_version WHERE version >= 10")
            await db.commit()

            # Second umbrella pass — handler re-runs against
            # already-altered interactions table; PRAGMA guard skips
            # ALTER, CREATE INDEX IF NOT EXISTS skips the index.
            await _apply_migrations(db)

            assert await _column_count(db, "interactions", "session_id") == 1
            assert await _index_exists(db, "idx_interactions_session")
            async with db.execute(
                "SELECT COUNT(*) FROM schema_version WHERE version = 10",
            ) as cursor:
                row = await cursor.fetchone()
            assert row is not None
            assert row[0] == 1
        finally:
            await db.close()

    async def test_direct_handler_replay_is_safe(self):
        """Direct handler invocation must be idempotent across two
        consecutive calls from a v9 baseline.
        """
        db = await aiosqlite.connect(":memory:")
        try:
            await _seed_v9_baseline(db)

            await _apply_migration_10(db)
            assert await _column_count(db, "interactions", "session_id") == 1
            assert await _index_exists(db, "idx_interactions_session")

            # Replay — must not raise, must not duplicate.
            await _apply_migration_10(db)
            assert await _column_count(db, "interactions", "session_id") == 1
            assert await _index_exists(db, "idx_interactions_session")
        finally:
            await db.close()


# ─── Empty-baseline guard ───────────────────────────────────


class TestEmptyTableGuard:
    """Partial-restore baseline: ``schema_version`` recorded up to v9
    but ``interactions`` table missing.  Handler must short-circuit
    cleanly — same contract as v7/v8/v9.
    """

    async def test_handler_no_op_on_missing_interactions(self):
        db = await aiosqlite.connect(":memory:")
        try:
            await db.execute(
                "CREATE TABLE schema_version "
                "(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL, "
                "description TEXT)",
            )
            # Record versions 1..9 as applied; do NOT create
            # interactions or any other v3 tables.
            for v in range(1, 10):
                await db.execute(
                    "INSERT INTO schema_version VALUES (?, ?, ?)",
                    (v, _time.time(), f"migration {v}"),
                )
            await db.commit()

            # Handler must not crash — no interactions table to ALTER.
            await _apply_migration_10(db)
        finally:
            await db.close()


# ─── Legacy upgrade ─────────────────────────────────────────


class TestLegacyUpgrade:
    async def test_upgrade_from_v9_backfills_legacy(self):
        """Existing interaction rows under a v9 baseline get the
        ``session_id='legacy'`` default after migration v10.
        """
        db = await aiosqlite.connect(":memory:")
        try:
            await _seed_v9_baseline(db)

            # Insert a pre-v10 interaction row.  Mirrors the v3+ shape
            # but without ``session_id``.
            await db.execute(
                "INSERT INTO interactions "
                "(id, participant_id, participant_type, "
                "other_participant_id, other_participant_type, "
                "interaction_type, outcome, sentiment, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "i1", "a", "agent", "b", "agent",
                    "collab", "ok", 0.0, _time.time(),
                ),
            )
            await db.commit()

            await _apply_migrations(db)

            async with db.execute(
                "SELECT session_id FROM interactions WHERE id = ?",
                ("i1",),
            ) as cursor:
                row = await cursor.fetchone()
            assert row is not None
            assert row[0] == "legacy"
        finally:
            await db.close()
