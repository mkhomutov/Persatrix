"""
Tests for the RFC 0020 §D schema migration (v5).

Covers:

* fresh-DB initialisation runs migration v5 and the new columns + index
  exist
* an in-place upgrade from a v4 baseline picks up the new columns
  without losing existing rows (NULL backfill per RFC 0020 §I)
* migration is idempotent — re-running ``_apply_migrations`` is a no-op
  and the column-existence guard prevents double-ALTER
* ``store_episode`` accepts the new keyword arguments and round-trips
  them through SQLite
* mixed-schema recall: legacy NULL-column rows continue to surface
  alongside new interaction rows
"""

from __future__ import annotations

import aiosqlite
import pytest

from agents.memory.episodic import EpisodicMemory
from agents.memory.migrations import (
    MIGRATIONS,
    _apply_migration_5,
    _apply_migration_6,
    _apply_migrations,
)


async def _episode_columns(db: aiosqlite.Connection) -> set[str]:
    cursor = await db.execute("PRAGMA table_info(episodes)")
    return {row[1] for row in await cursor.fetchall()}


async def _index_exists(db: aiosqlite.Connection, name: str) -> bool:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
        (name,),
    )
    return await cursor.fetchone() is not None


# ─── Fresh-DB migration ─────────────────────────────────────


class TestFreshSchemaMigration:
    async def test_migration_5_registered(self):
        versions = [v for v, _, _ in MIGRATIONS]
        assert 5 in versions

    async def test_new_columns_present_after_initialize(
        self, memory: EpisodicMemory,
    ):
        cols = await _episode_columns(memory._ensure_db())
        assert {
            "interaction_id", "started_at", "closed_at", "turn_count", "scope",
        }.issubset(cols)

    async def test_scope_index_created(self, memory: EpisodicMemory):
        assert await _index_exists(memory._ensure_db(), "idx_episodes_scope")

    async def test_schema_version_records_v5(self, memory: EpisodicMemory):
        async with memory._ensure_db().execute(
            "SELECT MAX(version) FROM schema_version",
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] >= 5


# ─── Idempotency ────────────────────────────────────────────


class TestMigrationIdempotency:
    async def test_double_apply_is_noop(self, memory: EpisodicMemory):
        db = memory._ensure_db()
        # Re-running the umbrella entry-point: no error, no extra rows in
        # ``schema_version`` (each version is recorded at most once).
        await _apply_migrations(db)
        await _apply_migrations(db)
        async with db.execute(
            "SELECT COUNT(*) FROM schema_version WHERE version = 5",
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] == 1

    async def test_direct_handler_replay_is_safe(self, memory: EpisodicMemory):
        # Simulate the crash-between-DDL-and-version-record case (RFC 0020
        # PR plan: schema migration must be idempotent).  Calling the
        # handler again on an already-migrated DB must not raise.
        db = memory._ensure_db()
        await _apply_migration_5(db)
        cols = await _episode_columns(db)
        # Still exactly one of each new column.
        assert sum(1 for c in cols if c == "interaction_id") == 1


# ─── Empty-baseline guard (no `episodes` table) ─────────────


class TestEmptyEpisodesGuard:
    """Regression for the v5 / v6 ``no-episodes-table`` early return.

    PR-1 review finding #4: an unusual but real shape during partial
    restores is a DB whose ``schema_version`` table records v1–v(n-1)
    but whose ``episodes`` table is missing.  ``ALTER TABLE`` on a
    non-existent table would raise; the handler instead detects the
    missing table via ``sqlite_master`` and returns without writing.

    Both v5 and v6 share this contract — they both ``ALTER TABLE
    episodes`` and both ship the same ``sqlite_master`` guard.  The
    handler-level test is parametrised over them so the v6 fix in
    PR #297 is pinned alongside v5: a future refactor that drops the
    guard from one but not the other would be caught here.  The
    umbrella test uses the latest registered migration so newly
    added no-op migrations also benefit from the same drift check.
    """

    @pytest.mark.parametrize(
        "handler, version",
        [
            (_apply_migration_5, 5),
            (_apply_migration_6, 6),
        ],
        ids=["v5", "v6"],
    )
    async def test_handler_no_op_on_missing_episodes_table(
        self, handler, version,
    ):
        # Simulate the partial-restore baseline: schema_version row at
        # v(version-1) but no ``episodes`` table.  The handler must
        # return cleanly, leave no episodes table behind, and not roll
        # back any other session state — the umbrella
        # ``_apply_migrations`` is the single owner of the version
        # record (parity with ``_apply_migration_4``'s "version-record
        # happens after this returns" contract).
        db = await aiosqlite.connect(":memory:")
        try:
            await db.execute(
                "CREATE TABLE schema_version "
                "(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL, "
                "description TEXT)",
            )
            await db.execute(
                "INSERT INTO schema_version VALUES (?, 0.0, 'baseline')",
                (version - 1,),
            )
            await db.commit()

            # Direct handler call — no exception, no episodes table created.
            await handler(db)

            cursor = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='episodes'",
            )
            assert await cursor.fetchone() is None

            # No ``idx_episodes_scope`` index either — v5 creates it on
            # the happy path; v6 never touches it.  Asserting absence
            # for both versions doubles as a "no v5 side-effects
            # leaked through the v6 path" check on the parametrised
            # iteration.
            cursor = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name='idx_episodes_scope'",
            )
            assert await cursor.fetchone() is None

            # Outer harness owns the version record — a direct handler
            # call must not touch ``schema_version``.
            cursor = await db.execute(
                "SELECT COUNT(*) FROM schema_version WHERE version = ?",
                (version,),
            )
            row = await cursor.fetchone()
            assert row[0] == 0
        finally:
            await db.close()

    async def test_umbrella_records_latest_even_on_no_op(self):
        # The umbrella ``_apply_migrations`` records v5+ as applied even
        # when the handlers short-circuited.  This is intentional — the
        # episodes table can be created by a later v1 re-run, and
        # blocking these versions from being recorded would loop the
        # upgrade.  Asserting against the latest registered migration
        # (and not just v5) keeps this test honest as new no-op-capable
        # migrations are appended: a future migration that the umbrella
        # silently skips on this baseline would be caught by the
        # ``MAX(version)`` check.
        latest = max(v for v, _, _ in MIGRATIONS)
        db = await aiosqlite.connect(":memory:")
        try:
            await db.execute(
                "CREATE TABLE schema_version "
                "(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL, "
                "description TEXT)",
            )
            await db.execute(
                "INSERT INTO schema_version VALUES (4, 0.0, 'v4 baseline')",
            )
            await db.commit()

            await _apply_migrations(db)

            # v5 is recorded exactly once — the original PR-297 contract.
            cursor = await db.execute(
                "SELECT COUNT(*) FROM schema_version WHERE version = 5",
            )
            row = await cursor.fetchone()
            assert row[0] == 1

            # And the umbrella reaches the latest registered migration —
            # protects against silently dropping a no-op migration when
            # new ones land on top of v6.
            cursor = await db.execute(
                "SELECT MAX(version) FROM schema_version",
            )
            row = await cursor.fetchone()
            assert row[0] == latest
        finally:
            await db.close()


# ─── Legacy upgrade path ────────────────────────────────────


class TestLegacyUpgrade:
    async def test_upgrade_from_v4_preserves_rows(self):
        """A DB pinned at v4 picks up v5 without losing pre-existing rows."""
        db = await aiosqlite.connect(":memory:")
        try:
            # Apply migrations v1–v4 by truncating MIGRATIONS in-place via
            # a direct loop — the production helper applies all migrations,
            # so we simulate the "v4 baseline" by running each entry up to
            # v4 manually.
            await db.execute(
                "CREATE TABLE schema_version "
                "(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL, "
                "description TEXT)",
            )
            import time as _time

            from agents.memory.migrations import (
                _MIGRATION_HANDLERS,
            )

            for version, desc, sql in MIGRATIONS:
                if version > 4:
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

            # Insert a legacy episode row (no RFC 0020 columns).
            await db.execute(
                """
                INSERT INTO episodes
                    (id, agent_id, summary, context_json, outcome,
                     importance, access_count, last_accessed_at,
                     tags_json, created_at, compressed_at, compression_level)
                VALUES (?, ?, ?, ?, ?, ?, 0, NULL, ?, ?, NULL, 0)
                """,
                (
                    "legacy-1", "agent-x", "old summary", "{}", None,
                    0.5, "[]", 1000.0,
                ),
            )
            await db.commit()

            # Run the umbrella migration runner — picks up v5.
            await _apply_migrations(db)

            cols = await _episode_columns(db)
            assert "interaction_id" in cols
            assert "scope" in cols

            # Legacy row preserved with NULL in new columns.
            async with db.execute(
                "SELECT id, summary, interaction_id, scope, turn_count "
                "FROM episodes WHERE id = ?",
                ("legacy-1",),
            ) as cursor:
                row = await cursor.fetchone()
            assert row is not None
            assert row[0] == "legacy-1"
            assert row[1] == "old summary"
            assert row[2] is None
            assert row[3] is None
            assert row[4] is None
        finally:
            await db.close()


# ─── store_episode round-trip ───────────────────────────────


class TestStoreEpisodeNewFields:
    async def test_default_call_writes_nulls(self, memory: EpisodicMemory):
        ep_id = await memory.store_episode("hello", {})
        async with memory._ensure_db().execute(
            "SELECT interaction_id, started_at, closed_at, turn_count, scope "
            "FROM episodes WHERE id = ?",
            (ep_id,),
        ) as cursor:
            row = await cursor.fetchone()
        assert row == (None, None, None, None, None)

    async def test_interaction_fields_round_trip(self, memory: EpisodicMemory):
        ep_id = await memory.store_episode(
            "negotiated outcome",
            {"k": "v"},
            interaction_id="ix-1",
            started_at=1000.0,
            closed_at=1300.0,
            turn_count=12,
            scope="dm:alice:bob",
        )
        async with memory._ensure_db().execute(
            "SELECT interaction_id, started_at, closed_at, turn_count, scope "
            "FROM episodes WHERE id = ?",
            (ep_id,),
        ) as cursor:
            row = await cursor.fetchone()
        assert row == ("ix-1", 1000.0, 1300.0, 12, "dm:alice:bob")

    async def test_mixed_legacy_and_new_rows_are_recallable(
        self, memory: EpisodicMemory,
    ):
        # Pre-RFC shape (no interaction columns).
        await memory.store_episode("legacy-style summary", {"src": "tick"})
        # Post-RFC shape.
        await memory.store_episode(
            "interaction summary",
            {"src": "dm"},
            interaction_id="ix-1",
            started_at=1.0,
            closed_at=2.0,
            turn_count=4,
            scope="dm:a:b",
        )
        results = await memory.recall(limit=10)
        summaries = {ep.summary for ep in results}
        assert "legacy-style summary" in summaries
        assert "interaction summary" in summaries


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
