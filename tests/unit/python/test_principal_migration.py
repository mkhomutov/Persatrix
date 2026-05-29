"""ISSUE-0081 PR 3 migration v11 — ``principal_id`` on all five tiers.

Migrations v7–v10 added ``session_id`` to ``episodes`` /
``relationships`` / ``facts`` / ``notes`` / ``interactions`` one or two
tables at a time; v11 adds the orthogonal **tenant** dimension
``principal_id`` to all five at once (RFC 0031 §C amendment).

Covers, mirroring :mod:`tests.unit.python.test_session_id_interactions_migration`:

* fresh-DB initialisation runs v11 and every tier has the
  ``principal_id`` column + ``idx_<tier>_principal`` index,
* the default value is :data:`agents.principal_id.DEFAULT_PRINCIPAL_ID`
  (``'local'``) — pre-existing rows upgrade with no backfill,
* ``schema_version`` records v11,
* umbrella replay (v11 row deleted) is idempotent,
* direct-handler replay from a v10 baseline is a clean no-op,
* the no-table partial-restore baseline does not crash the handler.
"""

from __future__ import annotations

import time as _time

import aiosqlite
import pytest

from agents.memory.episodic import EpisodicMemory
from agents.memory.migrations import (
    _MIGRATION_HANDLERS,
    MIGRATIONS,
    _apply_migration_11,
    _apply_migrations,
)
from agents.principal_id import DEFAULT_PRINCIPAL_ID

_TIERS = ("episodes", "relationships", "facts", "notes", "interactions")
_INDEXES = (
    "idx_episodes_principal",
    "idx_rel_principal",
    "idx_facts_principal",
    "idx_notes_principal",
    "idx_interactions_principal",
)


async def _columns(db: aiosqlite.Connection, table: str) -> set[str]:
    cursor = await db.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cursor.fetchall()}


async def _column_default(
    db: aiosqlite.Connection, table: str, name: str,
) -> str | None:
    cursor = await db.execute(f"PRAGMA table_info({table})")
    for row in await cursor.fetchall():
        if row[1] == name:
            return row[4]  # dflt_value
    return None


async def _index_exists(db: aiosqlite.Connection, name: str) -> bool:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
        (name,),
    )
    return await cursor.fetchone() is not None


async def _column_count(
    db: aiosqlite.Connection, table: str, name: str,
) -> int:
    cursor = await db.execute(f"PRAGMA table_info({table})")
    return sum(1 for row in await cursor.fetchall() if row[1] == name)


async def _seed_v10_baseline(db: aiosqlite.Connection) -> None:
    """Walk MIGRATIONS up to and including v10, recording each in
    ``schema_version`` — leaves the DB immediately before v11.
    """
    await db.execute(
        "CREATE TABLE IF NOT EXISTS schema_version "
        "(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL, "
        "description TEXT)",
    )
    for version, desc, sql in MIGRATIONS:
        if version > 10:
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
    async def test_migration_11_registered(self) -> None:
        versions = [v for v, _, _ in MIGRATIONS]
        assert 11 in versions

    @pytest.fixture
    async def memory(self):
        mem = EpisodicMemory(agent_id="t", db_path=":memory:")
        await mem.initialize()
        yield mem
        await mem.close()

    @pytest.mark.parametrize("table", _TIERS)
    async def test_principal_column_present(
        self, memory: EpisodicMemory, table: str,
    ) -> None:
        assert memory._db is not None
        cols = await _columns(memory._db, table)
        assert "principal_id" in cols, f"{table} missing principal_id"

    @pytest.mark.parametrize("index_name", _INDEXES)
    async def test_principal_index_created(
        self, memory: EpisodicMemory, index_name: str,
    ) -> None:
        assert memory._db is not None
        assert await _index_exists(memory._db, index_name)

    @pytest.mark.parametrize("table", _TIERS)
    async def test_principal_default_is_local(
        self, memory: EpisodicMemory, table: str,
    ) -> None:
        assert memory._db is not None
        default = await _column_default(memory._db, table, "principal_id")
        # PRAGMA reports the literal default including SQL quotes.
        assert default is not None
        assert DEFAULT_PRINCIPAL_ID in default

    async def test_schema_version_records_v11(
        self, memory: EpisodicMemory,
    ) -> None:
        assert memory._db is not None
        async with memory._db.execute(
            "SELECT COUNT(*) FROM schema_version WHERE version = 11",
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] == 1


# ─── In-place upgrade from a v10 baseline ───────────────────


class TestInPlaceUpgrade:
    async def test_v10_row_upgrades_to_local_principal(self) -> None:
        async with aiosqlite.connect(":memory:") as db:
            await _seed_v10_baseline(db)
            # A pre-v11 episode row (no principal_id column yet).
            await db.execute(
                "INSERT INTO episodes "
                "(id, agent_id, summary, created_at, session_id) "
                "VALUES ('e1', 'a', 's', ?, 'run-a')",
                (_time.time(),),
            )
            await db.commit()

            await _apply_migration_11(db)

            cols = await _columns(db, "episodes")
            assert "principal_id" in cols
            async with db.execute(
                "SELECT principal_id FROM episodes WHERE id='e1'",
            ) as cur:
                row = await cur.fetchone()
            assert row is not None
            assert row[0] == DEFAULT_PRINCIPAL_ID


# ─── Idempotency ────────────────────────────────────────────


class TestMigrationIdempotency:
    async def test_umbrella_replay_is_idempotent(self) -> None:
        """Deleting the v11 schema_version row (crash-between-DDL-and-record
        shape) and re-running _apply_migrations adds no duplicate column.
        """
        async with aiosqlite.connect(":memory:") as db:
            await _apply_migrations(db)
            await db.execute("DELETE FROM schema_version WHERE version=11")
            await db.commit()

            await _apply_migrations(db)

            for table in _TIERS:
                assert await _column_count(db, table, "principal_id") == 1

    async def test_direct_handler_replay_from_v10(self) -> None:
        async with aiosqlite.connect(":memory:") as db:
            await _seed_v10_baseline(db)
            await _apply_migration_11(db)
            # Second invocation must be a clean no-op.
            await _apply_migration_11(db)
            for table in _TIERS:
                assert await _column_count(db, table, "principal_id") == 1
                assert "principal_id" in await _columns(db, table)


# ─── Partial-restore baseline ───────────────────────────────


class TestPartialRestoreBaseline:
    async def test_no_tables_is_clean_noop(self) -> None:
        """A baseline with schema_version recorded but no tier tables
        (partial restore) must not crash the ADD COLUMN — same contract
        as v7/v9/v10.
        """
        async with aiosqlite.connect(":memory:") as db:
            await db.execute(
                "CREATE TABLE schema_version "
                "(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL, "
                "description TEXT)",
            )
            await db.commit()
            # Must not raise even though none of the five tables exist.
            await _apply_migration_11(db)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
