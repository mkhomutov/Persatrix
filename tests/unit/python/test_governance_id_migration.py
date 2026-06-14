"""Tests for the ISSUE-0102 PR 2 schema migration (v15) — the queryable
``governance_interaction_id`` column on ``episodes``.

PR 1 persisted the RFC 0030 governance interaction id into the episode
*context blob* (display only). PR 2 promotes it to a real column so the
closed-interaction read filter can match it, making the channel-side id
directly look-up-able via ``agent interactions --interaction-id <gov-id>``.

Covers:

* fresh-DB initialisation runs v15 and the new column exists
* an in-place upgrade from a v14 baseline picks up the column and
  **backfills** it from each row's ``context_json`` (the PR 1 shape)
* a context blob with no governance id leaves the column ``NULL``
* migration is idempotent — re-running ``_apply_migrations`` / the handler
  is a no-op
* the empty-baseline guard (no ``episodes`` table) is a clean no-op
* ``store_episode`` accepts the new keyword and round-trips it to the column
"""

from __future__ import annotations

import json

import aiosqlite
import pytest

from agents.memory.episodic import EpisodicMemory
from agents.memory.migrations import (
    MIGRATIONS,
    _apply_migration_15,
    _apply_migrations,
)


async def _episode_columns(db: aiosqlite.Connection) -> set[str]:
    cursor = await db.execute("PRAGMA table_info(episodes)")
    return {row[1] for row in await cursor.fetchall()}


# ─── Fresh-DB migration ─────────────────────────────────────


class TestFreshSchemaMigration:
    async def test_migration_15_registered(self):
        versions = [v for v, _, _ in MIGRATIONS]
        assert 15 in versions

    async def test_governance_column_present_after_initialize(
        self, memory: EpisodicMemory,
    ):
        cols = await _episode_columns(memory._ensure_db())
        assert "governance_interaction_id" in cols

    async def test_schema_version_records_v15(self, memory: EpisodicMemory):
        async with memory._ensure_db().execute(
            "SELECT MAX(version) FROM schema_version",
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] >= 15


# ─── Idempotency ────────────────────────────────────────────


class TestMigrationIdempotency:
    async def test_double_apply_is_noop(self, memory: EpisodicMemory):
        db = memory._ensure_db()
        await _apply_migrations(db)
        await _apply_migrations(db)
        async with db.execute(
            "SELECT COUNT(*) FROM schema_version WHERE version = 15",
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] == 1

    async def test_direct_handler_replay_is_safe(self, memory: EpisodicMemory):
        db = memory._ensure_db()
        await _apply_migration_15(db)
        cols = await _episode_columns(db)
        assert sum(1 for c in cols if c == "governance_interaction_id") == 1


# ─── Empty-baseline guard (no `episodes` table) ─────────────


class TestEmptyEpisodesGuard:
    async def test_handler_no_op_on_missing_episodes_table(self):
        db = await aiosqlite.connect(":memory:")
        try:
            await db.execute(
                "CREATE TABLE schema_version "
                "(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL, "
                "description TEXT)",
            )
            await db.execute(
                "INSERT INTO schema_version VALUES (14, 0.0, 'baseline')",
            )
            await db.commit()
            # No episodes table → handler must return cleanly.
            await _apply_migration_15(db)
            cursor = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='episodes'",
            )
            assert await cursor.fetchone() is None
            cursor = await db.execute(
                "SELECT COUNT(*) FROM schema_version WHERE version = 15",
            )
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == 0
        finally:
            await db.close()


# ─── Legacy upgrade + backfill from context_json ────────────


class TestLegacyUpgradeBackfill:
    async def _v14_baseline(self) -> aiosqlite.Connection:
        """A DB migrated to v14 (one short of v15)."""
        db = await aiosqlite.connect(":memory:")
        await db.execute(
            "CREATE TABLE schema_version "
            "(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL, "
            "description TEXT)",
        )
        import time as _time

        from agents.memory.migrations import _MIGRATION_HANDLERS

        for version, desc, sql in MIGRATIONS:
            if version > 14:
                continue
            handler = _MIGRATION_HANDLERS.get(version)
            if handler is not None:
                await handler(db)
            elif sql:
                await db.executescript(sql)
            await db.execute(
                "INSERT INTO schema_version VALUES (?, ?, ?)",
                (version, _time.time(), desc),
            )
        await db.commit()
        return db

    async def _insert_row(
        self, db: aiosqlite.Connection, *, ep_id: str, context: dict,
    ) -> None:
        await db.execute(
            """
            INSERT INTO episodes
                (id, agent_id, summary, context_json, outcome,
                 importance, access_count, last_accessed_at,
                 tags_json, created_at, compressed_at, compression_level)
            VALUES (?, ?, ?, ?, ?, ?, 0, NULL, ?, ?, NULL, 0)
            """,
            (ep_id, "agent-x", "s", json.dumps(context), None,
             0.5, "[]", 1000.0),
        )

    async def test_backfills_governance_id_from_context(self):
        db = await self._v14_baseline()
        try:
            # A PR-1-shaped row: governance id lives only in context_json.
            await self._insert_row(
                db, ep_id="e-gov",
                context={"scope": "group:room", "close_reason": "structural",
                         "governance_interaction_id": "gov-4b332af1"},
            )
            await db.commit()
            await _apply_migrations(db)  # picks up v15

            async with db.execute(
                "SELECT governance_interaction_id FROM episodes WHERE id = ?",
                ("e-gov",),
            ) as cursor:
                row = await cursor.fetchone()
            assert row[0] == "gov-4b332af1"
        finally:
            await db.close()

    async def test_row_without_governance_id_stays_null(self):
        db = await self._v14_baseline()
        try:
            await self._insert_row(
                db, ep_id="e-plain",
                context={"scope": "dm:a:b", "close_reason": "idle_gap"},
            )
            await db.commit()
            await _apply_migrations(db)

            async with db.execute(
                "SELECT governance_interaction_id FROM episodes WHERE id = ?",
                ("e-plain",),
            ) as cursor:
                row = await cursor.fetchone()
            assert row[0] is None
        finally:
            await db.close()


# ─── store_episode round-trip ───────────────────────────────


class TestStoreEpisodeGovernanceField:
    async def test_default_call_writes_null(self, memory: EpisodicMemory):
        ep_id = await memory.store_episode("hello", {})
        async with memory._ensure_db().execute(
            "SELECT governance_interaction_id FROM episodes WHERE id = ?",
            (ep_id,),
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] is None

    async def test_governance_id_round_trips(self, memory: EpisodicMemory):
        ep_id = await memory.store_episode(
            "converged", {"k": "v"},
            interaction_id="ix-1", governance_interaction_id="gov-1",
            started_at=1.0, closed_at=2.0, turn_count=3, scope="group:r",
        )
        async with memory._ensure_db().execute(
            "SELECT interaction_id, governance_interaction_id "
            "FROM episodes WHERE id = ?",
            (ep_id,),
        ) as cursor:
            row = await cursor.fetchone()
        assert row == ("ix-1", "gov-1")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
