"""Tests for the ISSUE-0131 schema migration (v18) — the ``speaker_id``
column on the two close-derived tiers (``episodes`` / ``facts``).

Covers, in the v16 protection-migration suite's shape:

* fresh-DB initialisation runs v18: ``speaker_id`` exists on
  ``episodes`` and ``facts`` — and deliberately NOT on ``notes``
  (reflection-written, not close-derived) or the ``interactions``
  TABLE (the DM-only relationship log; the residuals plan names both
  as the wrong nearby targets)
* an in-place upgrade from a populated v17 baseline leaves every
  pre-existing row's ``speaker_id`` NULL — a pre-split aggregate's
  speaker is unknowable without the model-elected attribution the
  Phase 0b scope lock forbids, so unlike v11's ``DEFAULT 'local'``
  there is no backfill
* the migration is idempotent (re-running the handler is a no-op) and
  tolerates a partial-restore baseline missing a tier table
* the column is DORMANT: no index (attribution surface, not a recall
  filter — the v13 precedent), and the writer is residuals PR 4
"""

from __future__ import annotations

import aiosqlite
import pytest

from agents.memory.episodic import EpisodicMemory
from agents.memory.migrations import (
    MIGRATIONS,
    _apply_migration_18,
    _apply_migrations,
)

_SPEAKER_TIERS = ("episodes", "facts")


async def _columns(db: aiosqlite.Connection, table: str) -> set[str]:
    cursor = await db.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cursor.fetchall()}


@pytest.fixture
async def memory():
    mem = EpisodicMemory(agent_id="test-agent", db_path=":memory:")
    await mem.initialize()
    yield mem
    await mem.close()


class TestFreshSchemaMigration:
    async def test_migration_18_registered(self):
        versions = [v for v, _, _ in MIGRATIONS]
        assert 18 in versions

    async def test_speaker_id_on_both_close_derived_tiers(
        self, memory: EpisodicMemory,
    ):
        db = memory._ensure_db()
        for table in _SPEAKER_TIERS:
            cols = await _columns(db, table)
            assert "speaker_id" in cols, f"{table} is missing speaker_id"

    async def test_wrong_nearby_targets_stay_untouched(
        self, memory: EpisodicMemory,
    ):
        """The residuals plan names them explicitly: ``notes`` is
        reflection-written and the ``interactions`` TABLE is the DM-only
        relationship log — neither is a group-close write target, so
        neither gains the column."""
        db = memory._ensure_db()
        for table in ("notes", "interactions"):
            cols = await _columns(db, table)
            assert "speaker_id" not in cols, (
                f"{table} must not grow speaker_id — it is not a "
                "close-derived tier"
            )

    async def test_no_speaker_index(self, memory: EpisodicMemory):
        """Attribution surface, not a recall filter (module docstring;
        the v13 ``identity`` precedent) — a per-speaker recall predicate
        adds its own index in its own PR."""
        async with memory._ensure_db().execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND sql LIKE '%speaker_id%'",
        ) as cursor:
            assert await cursor.fetchone() is None

    async def test_schema_version_records_v18(self, memory: EpisodicMemory):
        async with memory._ensure_db().execute(
            "SELECT MAX(version) FROM schema_version",
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] >= 18


class TestUpgradeFromV17Baseline:
    async def test_pre_existing_rows_stay_null(self):
        """The no-backfill contract: rows derived before the speaker
        axis existed read NULL — never a guessed speaker.

        The v17 baseline is built by applying the REAL migrations capped
        at v17 (the retention suite's MIGRATIONS-patching precedent) —
        not by ``DROP COLUMN``, a SQLite ≥ 3.35 feature the migration
        itself deliberately does not require (PR #846 review): the old
        shape made this contract unverifiable exactly on the platforms
        the guard skeleton exists for."""
        db = await aiosqlite.connect(":memory:")
        try:
            original = list(MIGRATIONS)
            MIGRATIONS[:] = [m for m in MIGRATIONS if m[0] <= 17]
            try:
                await _apply_migrations(db)
            finally:
                MIGRATIONS[:] = original
            assert "speaker_id" not in await _columns(db, "episodes"), (
                "the capped apply must yield a genuine pre-v18 baseline"
            )
            await db.execute(
                "INSERT INTO episodes (id, agent_id, summary, created_at) "
                "VALUES ('ep-1', 'a1', 'pre-v18 row', 1000.0)",
            )
            await db.commit()

            await _apply_migration_18(db)

            async with db.execute(
                "SELECT speaker_id FROM episodes WHERE id = 'ep-1'",
            ) as cursor:
                row = await cursor.fetchone()
            assert row is not None
            assert row[0] is None, "pre-v18 rows must stay NULL (no backfill)"
        finally:
            await db.close()

    async def test_handler_is_idempotent(self, memory: EpisodicMemory):
        db = memory._ensure_db()
        await _apply_migration_18(db)  # re-run against an applied schema
        for table in _SPEAKER_TIERS:
            cols = await _columns(db, table)
            assert "speaker_id" in cols

    async def test_partial_restore_missing_table_is_tolerated(self):
        """The v7/v13 guard shape: a baseline missing a tier table
        short-circuits that half cleanly instead of crashing the boot."""
        db = await aiosqlite.connect(":memory:")
        try:
            await db.execute(
                "CREATE TABLE episodes (id TEXT PRIMARY KEY, "
                "agent_id TEXT NOT NULL, summary TEXT NOT NULL, "
                "created_at REAL NOT NULL)",
            )
            await db.commit()

            await _apply_migration_18(db)  # no ``facts`` table at all

            assert "speaker_id" in await _columns(db, "episodes")
        finally:
            await db.close()
