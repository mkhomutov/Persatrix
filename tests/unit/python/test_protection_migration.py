"""Tests for the RFC 0037 PR 3 schema migration (v16) — the §C
protection/provenance columns on the three channel-derived tiers plus the
§E ``memory_projections`` table.

Covers:

* fresh-DB initialisation runs v16: ``protection_level`` /
  ``source_channel_id`` / ``provenance_json`` exist on ``episodes``,
  ``facts``, and ``notes``; ``memory_projections`` exists with its
  ``(entry_id, entry_tier, level)`` primary key
* an in-place upgrade from a populated v15 baseline **backfills**
  ``protection_level = 'internal'`` on every pre-existing row (the §C
  backfill — neither silently ``public`` nor silently ``secret``) and
  leaves ``source_channel_id`` / ``provenance_json`` NULL
* migration is idempotent — re-running ``_apply_migrations`` / the handler
  is a no-op
* the empty-baseline guard (no tier tables) still creates
  ``memory_projections`` and returns cleanly
* ``store_episode`` / ``FactStore.store`` accept the new keywords and
  round-trip them to the columns; omitting them stamps the ``internal``
  default (no path writes an entry without a protection level)
"""

from __future__ import annotations

import aiosqlite
import pytest

from agents.memory.episodic import EpisodicMemory
from agents.memory.facts import FactStore
from agents.memory.migrations import (
    MIGRATIONS,
    PROTECTION_LEVEL_DEFAULT,
    _apply_migration_16,
    _apply_migrations,
)

_NEW_COLUMNS = {"protection_level", "source_channel_id", "provenance_json"}
_TIERS = ("episodes", "facts", "notes")


async def _columns(db: aiosqlite.Connection, table: str) -> set[str]:
    cursor = await db.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cursor.fetchall()}


# ─── Fresh-DB migration ─────────────────────────────────────


class TestFreshSchemaMigration:
    async def test_migration_16_registered(self):
        versions = [v for v, _, _ in MIGRATIONS]
        assert 16 in versions

    async def test_columns_present_on_all_three_tiers(
        self, memory: EpisodicMemory,
    ):
        db = memory._ensure_db()
        for table in _TIERS:
            cols = await _columns(db, table)
            assert _NEW_COLUMNS <= cols, (
                f"{table} is missing {_NEW_COLUMNS - cols}"
            )

    async def test_memory_projections_table_and_pk(
        self, memory: EpisodicMemory,
    ):
        db = memory._ensure_db()
        cols = await _columns(db, "memory_projections")
        assert cols == {"entry_id", "entry_tier", "level", "text", "created_at"}
        cursor = await db.execute("PRAGMA table_info(memory_projections)")
        pk = {row[1] for row in await cursor.fetchall() if row[5] > 0}
        assert pk == {"entry_id", "entry_tier", "level"}

    async def test_schema_version_records_v16(self, memory: EpisodicMemory):
        async with memory._ensure_db().execute(
            "SELECT MAX(version) FROM schema_version",
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] >= 16


# ─── Idempotency ────────────────────────────────────────────


class TestMigrationIdempotency:
    async def test_double_apply_is_noop(self, memory: EpisodicMemory):
        db = memory._ensure_db()
        await _apply_migrations(db)
        await _apply_migrations(db)
        async with db.execute(
            "SELECT COUNT(*) FROM schema_version WHERE version = 16",
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] == 1

    async def test_direct_handler_replay_is_safe(self, memory: EpisodicMemory):
        db = memory._ensure_db()
        await _apply_migration_16(db)
        for table in _TIERS:
            cols_list = [
                row[1]
                for row in await (
                    await db.execute(f"PRAGMA table_info({table})")
                ).fetchall()
            ]
            assert cols_list.count("protection_level") == 1


# ─── Empty-baseline guard ───────────────────────────────────


class TestEmptyBaselineGuard:
    async def test_handler_no_op_on_missing_tier_tables(self):
        db = await aiosqlite.connect(":memory:")
        try:
            # No episodes/facts/notes tables at all — the handler must
            # skip the ADD COLUMN halves cleanly and still create the
            # (table-independent) projections table.
            await _apply_migration_16(db)
            cursor = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='memory_projections'",
            )
            assert await cursor.fetchone() is not None
        finally:
            await db.close()


# ─── Populated-v15 upgrade + internal backfill ──────────────


class TestPopulatedUpgradeBackfill:
    async def _v15_baseline(self) -> aiosqlite.Connection:
        """A DB migrated to v15 (one short of v16) with one row per tier."""
        db = await aiosqlite.connect(":memory:")
        await db.execute(
            "CREATE TABLE schema_version "
            "(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL, "
            "description TEXT)",
        )
        from agents.memory.migrations import _MIGRATION_HANDLERS

        for version, desc, sql in MIGRATIONS:
            if version > 15:
                continue
            handler = _MIGRATION_HANDLERS.get(version)
            if handler is not None:
                await handler(db)
            elif sql:
                await db.executescript(sql)
            await db.execute(
                "INSERT INTO schema_version VALUES (?, ?, ?)",
                (version, 0.0, desc),
            )
        await db.execute(
            "INSERT INTO episodes (id, agent_id, summary, created_at) "
            "VALUES ('e-1', 'agent-x', 's', 1000.0)",
        )
        await db.execute(
            "INSERT INTO facts (fact_id, agent_id, subject, predicate, "
            "object, asserted_at) "
            "VALUES ('f-1', 'agent-x', 'alice', 'has_name', 'Alice', 1000.0)",
        )
        await db.execute(
            "INSERT INTO notes (id, agent_id, topic, content, created_at, "
            "updated_at) VALUES ('n-1', 'agent-x', 't', 'c', 1000.0, 1000.0)",
        )
        await db.commit()
        return db

    async def test_pre_existing_rows_backfill_internal(self):
        db = await self._v15_baseline()
        try:
            await _apply_migrations(db)  # picks up v16
            for table, id_col, row_id in (
                ("episodes", "id", "e-1"),
                ("facts", "fact_id", "f-1"),
                ("notes", "id", "n-1"),
            ):
                async with db.execute(
                    f"SELECT protection_level, source_channel_id, "
                    f"provenance_json FROM {table} WHERE {id_col} = ?",
                    (row_id,),
                ) as cursor:
                    row = await cursor.fetchone()
                assert row == (PROTECTION_LEVEL_DEFAULT, None, None), (
                    f"{table} backfill mismatch: {row!r}"
                )
        finally:
            await db.close()


# ─── Write-API round-trips ──────────────────────────────────


class TestStoreEpisodeProtectionFields:
    async def test_default_call_stamps_internal(self, memory: EpisodicMemory):
        ep_id = await memory.store_episode("hello", {})
        async with memory._ensure_db().execute(
            "SELECT protection_level, source_channel_id FROM episodes "
            "WHERE id = ?",
            (ep_id,),
        ) as cursor:
            row = await cursor.fetchone()
        assert row == (PROTECTION_LEVEL_DEFAULT, None)

    async def test_protection_fields_round_trip(self, memory: EpisodicMemory):
        ep_id = await memory.store_episode(
            "restricted content", {},
            protection_level="restricted", source_channel_id="grp-ops",
        )
        async with memory._ensure_db().execute(
            "SELECT protection_level, source_channel_id FROM episodes "
            "WHERE id = ?",
            (ep_id,),
        ) as cursor:
            row = await cursor.fetchone()
        assert row == ("restricted", "grp-ops")


class TestFactStoreProtectionFields:
    async def _facts(self) -> FactStore:
        store = FactStore(agent_id="agent-x", db_path=":memory:")
        await store.initialize()
        return store

    async def test_default_call_stamps_internal(self):
        store = await self._facts()
        try:
            fact_id = await store.store(
                subject="alice", predicate="has_name", object="Alice",
                source_interaction_id=None, asserted_at=1000.0,
            )
            async with store._ensure_db().execute(
                "SELECT protection_level, source_channel_id FROM facts "
                "WHERE fact_id = ?",
                (fact_id,),
            ) as cursor:
                row = await cursor.fetchone()
            assert row == (PROTECTION_LEVEL_DEFAULT, None)
        finally:
            await store.close()

    async def test_protection_fields_round_trip(self):
        store = await self._facts()
        try:
            fact_id = await store.store(
                subject="alice", predicate="has_name", object="Alice",
                source_interaction_id="ix-1", asserted_at=1000.0,
                protection_level="secret", source_channel_id="grp-warroom",
            )
            async with store._ensure_db().execute(
                "SELECT protection_level, source_channel_id FROM facts "
                "WHERE fact_id = ?",
                (fact_id,),
            ) as cursor:
                row = await cursor.fetchone()
            assert row == ("secret", "grp-warroom")
        finally:
            await store.close()

    async def test_superseding_row_restamps_from_its_own_source(self):
        """§C item 3: latest-asserted-wins extends to classification — a
        superseding assertion is a NEW row stamped from its own source, up
        or down; the superseded row keeps its original stamp."""
        store = await self._facts()
        try:
            first = await store.store(
                subject="alice", predicate="has_name", object="Alice",
                source_interaction_id="ix-1", asserted_at=1000.0,
                protection_level="secret", source_channel_id="grp-warroom",
            )
            second = await store.store(
                subject="alice", predicate="has_name", object="Alicia",
                source_interaction_id="ix-2", asserted_at=2000.0,
                protection_level="public", source_channel_id="grp-town",
            )
            db = store._ensure_db()
            async with db.execute(
                "SELECT protection_level, superseded_by FROM facts "
                "WHERE fact_id = ?", (first,),
            ) as cursor:
                old = await cursor.fetchone()
            async with db.execute(
                "SELECT protection_level, superseded_by FROM facts "
                "WHERE fact_id = ?", (second,),
            ) as cursor:
                new = await cursor.fetchone()
            assert old == ("secret", second)
            assert new == ("public", None)
        finally:
            await store.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
