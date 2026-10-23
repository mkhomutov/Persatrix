"""Tests for RFC 0021 Phase 2 PR 1 — memory-store migration v20, the
``commitments`` table.

One table holds two kinds of row (the Phase 2 plan's H-2): a persona's
own promise (``kind = 'commitment'``) and an instruction someone gave it
(``kind = 'directive'``, RFC 0012 §E's influence record).  Covers:

* a fresh store has the table, every column the plan names and the three
  indexes, and records v20
* the scope columns default exactly as the facts tier's do, so a row
  written without them is scoped as a fact written without them is
* an in-place upgrade from a populated v19 store adds the table and
  leaves every earlier row and schema object as it was
* the handler is idempotent and leaves a pre-existing table alone

The check constraints are tested in ``test_commitments_constraints.py``.

Nothing reads the table yet: PR 2's ``CommitmentStore`` is its first
reader.
"""

from __future__ import annotations

from typing import Any

import aiosqlite
import pytest

from agents.epoch_id import DEFAULT_EPOCH_ID
from agents.memory._migration_commitments import _apply_migration_20
from agents.memory.episodic import EpisodicMemory
from agents.memory.migrations import (
    MIGRATIONS,
    PROTECTION_LEVEL_DEFAULT,
    _apply_migrations,
)
from agents.principal_id import DEFAULT_PRINCIPAL_ID
from agents.session_id import LEGACY_SESSION_ID

from ._commitments_migration_helpers import COMMITMENT, DIRECTIVE, insert

_SCOPE_COLUMNS = ("session_id", "principal_id", "epoch_id", "protection_level")

# ``(type, notnull, default)`` of every other column, as ``PRAGMA
# table_info`` reports it.  The scope columns are pinned against the facts
# tier instead (``TestScopeColumns``).
_DECLARED: dict[str, tuple[str, int, str | None]] = {
    # RFC 0021 §F
    "id": ("TEXT", 1, None),
    "agent_id": ("TEXT", 1, None),
    "kind": ("TEXT", 1, None),
    "description": ("TEXT", 1, None),
    "due_at": ("REAL", 0, None),
    "target_party": ("TEXT", 0, None),
    "created_at": ("REAL", 1, None),
    "state": ("TEXT", 0, None),
    "fulfilled_at": ("REAL", 0, None),
    "source_interaction_id": ("TEXT", 0, None),
    "tags_json": ("TEXT", 1, "'[]'"),
    # RFC 0012 §E, and the RFC 0012 plan's asks of row A2
    "issuer_id": ("TEXT", 0, None),
    "issuing_channel_id": ("TEXT", 0, None),
    "channel_authority": ("TEXT", 0, None),
    "issuer_authority": ("TEXT", 0, None),
    "scope": ("TEXT", 0, None),
    "directive_status": ("TEXT", 0, None),
    "polarity": ("TEXT", 0, None),
    "action_class": ("TEXT", 0, None),
    "target": ("TEXT", 0, None),
    "graph_version": ("INTEGER", 0, None),
    "conflicts_with_json": ("TEXT", 1, "'[]'"),
    "conflict_checked_at": ("REAL", 0, None),
    "conflict_failures": ("INTEGER", 1, "0"),
}

_INDEX_COLUMNS: dict[str, list[str]] = {
    # the open-commitments block: open rows, nearest due first
    "idx_commitments_due": ["agent_id", "kind", "state", "due_at"],
    # RFC 0012's reads, by directive status
    "idx_commitments_directive": ["agent_id", "kind", "directive_status"],
    # ``list_commitments_to(target_party)``
    "idx_commitments_target": ["agent_id", "target_party", "state"],
}


async def _column_info(
    db: aiosqlite.Connection, table: str,
) -> dict[str, tuple[str, int, str | None]]:
    """``{name: (type, notnull, default)}`` from ``PRAGMA table_info``."""
    cursor = await db.execute(f"PRAGMA table_info({table})")
    return {
        row[1]: (row[2], row[3], row[4]) for row in await cursor.fetchall()
    }


async def _index_columns(db: aiosqlite.Connection, index: str) -> list[str]:
    cursor = await db.execute(f"PRAGMA index_info({index})")
    return [row[2] for row in sorted(await cursor.fetchall())]


async def _dump(db: aiosqlite.Connection) -> dict[str, list[tuple[Any, ...]]]:
    """Every row of every ordinary table, plus every schema object."""
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '%_fts%' "
        "AND name != 'schema_version' ORDER BY name",
    )
    tables = [row[0] for row in await cursor.fetchall()]
    dump: dict[str, list[tuple[Any, ...]]] = {}
    for table in tables:
        cursor = await db.execute(f"SELECT * FROM {table} ORDER BY rowid")  # noqa: S608 — names from sqlite_master
        dump[table] = [tuple(row) for row in await cursor.fetchall()]
    cursor = await db.execute(
        "SELECT type, name, sql FROM sqlite_master ORDER BY type, name",
    )
    dump["sqlite_master"] = [tuple(row) for row in await cursor.fetchall()]
    return dump


async def _v19_store() -> aiosqlite.Connection:
    """A genuine v19 store, built by the real migrations capped at v19."""
    db = await aiosqlite.connect(":memory:")
    original = list(MIGRATIONS)
    MIGRATIONS[:] = [m for m in MIGRATIONS if m[0] <= 19]
    try:
        await _apply_migrations(db)
    finally:
        MIGRATIONS[:] = original
    return db


# ─── Fresh store ────────────────────────────────────────────


class TestFreshStore:
    async def test_migration_20_registered(self):
        versions = [v for v, _, _ in MIGRATIONS]
        assert 20 in versions

    async def test_columns(self, memory: EpisodicMemory):
        """Every column, with its type, NOT NULL and default — so a
        dropped NOT NULL fails here, not in PR 2's first write."""
        info = await _column_info(memory._ensure_db(), "commitments")
        assert set(info) == {*_DECLARED, *_SCOPE_COLUMNS}, (
            f"missing: {set(_DECLARED) - set(info)}; "
            f"unexpected: {set(info) - set(_DECLARED) - set(_SCOPE_COLUMNS)}"
        )
        assert {name: info[name] for name in _DECLARED} == _DECLARED

    @pytest.mark.parametrize("index, columns", list(_INDEX_COLUMNS.items()))
    async def test_indexes(
        self, memory: EpisodicMemory, index: str, columns: list[str],
    ):
        assert await _index_columns(memory._ensure_db(), index) == columns

    @pytest.mark.parametrize(
        "query, index",
        [
            (
                "SELECT id FROM commitments WHERE agent_id = 'a' "
                "AND kind = 'commitment' AND state = 'open' "
                "ORDER BY due_at",
                "idx_commitments_due",
            ),
            (
                "SELECT id FROM commitments WHERE agent_id = 'a' "
                "AND kind = 'directive' AND directive_status = 'pending'",
                "idx_commitments_directive",
            ),
            (
                "SELECT id FROM commitments WHERE agent_id = 'a' "
                "AND kind = 'commitment' AND target_party = 'bob'",
                "idx_commitments_target",
            ),
        ],
    )
    async def test_reads_use_their_index(
        self, memory: EpisodicMemory, query: str, index: str,
    ):
        cursor = await memory._ensure_db().execute(
            f"EXPLAIN QUERY PLAN {query}",
        )
        plan = " ".join(row[3] for row in await cursor.fetchall())
        assert index in plan, plan
        assert "TEMP B-TREE" not in plan, plan

    async def test_records_v20(self, memory: EpisodicMemory):
        async with memory._ensure_db().execute(
            "SELECT description FROM schema_version WHERE version = 20",
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert "commitments" in row[0]


# ─── Scope columns ──────────────────────────────────────────


class TestScopeColumns:
    @pytest.mark.parametrize("column", _SCOPE_COLUMNS)
    async def test_declared_as_the_facts_tier_declares_it(
        self, memory: EpisodicMemory, column: str,
    ):
        """Same type, NOT NULL and default as ``facts`` — so PR 2 can
        read the table with the facts tier's own filters."""
        db = memory._ensure_db()
        facts = await _column_info(db, "facts")
        commitments = await _column_info(db, "commitments")
        assert commitments[column] == facts[column]

    async def test_row_written_without_them(self, memory: EpisodicMemory):
        db = memory._ensure_db()
        await insert(db, COMMITMENT)
        async with db.execute(
            "SELECT session_id, principal_id, epoch_id, protection_level "
            "FROM commitments WHERE id = 'c-1'",
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert tuple(row) == (
            LEGACY_SESSION_ID,
            DEFAULT_PRINCIPAL_ID,
            DEFAULT_EPOCH_ID,
            PROTECTION_LEVEL_DEFAULT,
        )

    async def test_list_and_count_defaults(self, memory: EpisodicMemory):
        db = memory._ensure_db()
        await insert(db, DIRECTIVE)
        async with db.execute(
            "SELECT tags_json, conflicts_with_json, conflict_failures, "
            "conflict_checked_at FROM commitments WHERE id = 'd-1'",
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert tuple(row) == ("[]", "[]", 0, None)


# ─── Upgrade from a populated v19 store ─────────────────────


class TestUpgradeFromV19:
    async def test_adds_table_and_leaves_earlier_rows(self):
        db = await _v19_store()
        try:
            assert await _column_info(db, "commitments") == {}
            async with db.execute(
                "SELECT MAX(version) FROM schema_version",
            ) as cursor:
                row = await cursor.fetchone()
            assert row is not None
            assert row[0] == 19, "the capped apply must yield a v19 store"

            await db.execute(
                "INSERT INTO episodes (id, agent_id, summary, created_at, "
                "session_id, principal_id, epoch_id) "
                "VALUES ('ep-1', 'ember-owl', 'met Bob', 900.0, 'run-a', "
                "'tenant-a', 'epoch-a')",
            )
            await db.execute(
                "INSERT INTO facts (fact_id, agent_id, subject, predicate, "
                "object, asserted_at, protection_level) "
                "VALUES ('f-1', 'ember-owl', 'bob', 'works_at', 'Acme', "
                "900.0, 'restricted')",
            )
            await db.execute(
                "INSERT INTO notes (id, agent_id, topic, content, "
                "created_at, updated_at) "
                "VALUES ('n-1', 'ember-owl', 'bob', 'likes tea', 900.0, "
                "900.0)",
            )
            await db.commit()
            before = await _dump(db)

            await _apply_migrations(db)

            after = await _dump(db)
            assert after.pop("commitments") == []
            new_objects = [
                obj for obj in after["sqlite_master"]
                if obj not in before["sqlite_master"]
            ]
            assert sorted(name for _, name, _ in new_objects) == [
                "commitments",
                "idx_commitments_directive",
                "idx_commitments_due",
                "idx_commitments_target",
                "sqlite_autoindex_commitments_1",  # the TEXT primary key
            ]
            after["sqlite_master"] = [
                obj for obj in after["sqlite_master"]
                if obj not in new_objects
            ]
            assert after == before
            async with db.execute(
                "SELECT MAX(version) FROM schema_version",
            ) as cursor:
                row = await cursor.fetchone()
            assert row is not None
            assert row[0] == 20
        finally:
            await db.close()


# ─── Idempotency and partial restores ───────────────────────


class TestIdempotency:
    async def test_handler_replay_keeps_rows_and_rebuilds_indexes(
        self, memory: EpisodicMemory,
    ):
        """The crash after the table and before its indexes — SQLite
        commits each CREATE on its own — then the version record never
        written: the replay keeps the rows and builds the missing indexes."""
        db = memory._ensure_db()
        await insert(db, COMMITMENT)
        for index in _INDEX_COLUMNS:
            await db.execute(f"DROP INDEX {index}")
        await db.commit()

        await _apply_migration_20(db)

        async with db.execute("SELECT id FROM commitments") as cursor:
            rows = [r[0] for r in await cursor.fetchall()]
        assert rows == ["c-1"]
        for index, columns in _INDEX_COLUMNS.items():
            assert await _index_columns(db, index) == columns

    async def test_umbrella_rerun_records_v20_once(
        self, memory: EpisodicMemory,
    ):
        """The version gate: a second ``_apply_migrations`` skips v20
        rather than recording it twice (the handler's own replay is the
        test above)."""
        db = memory._ensure_db()
        await _apply_migrations(db)
        async with db.execute(
            "SELECT COUNT(*) FROM schema_version WHERE version = 20",
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] == 1

    async def test_stub_table_left_alone(self):
        """A partial restore that already holds a ``commitments`` table of
        another shape: the handler neither drops it nor indexes columns
        it lacks, and the umbrella still records v20."""
        db = await aiosqlite.connect(":memory:")
        try:
            await db.execute(
                "CREATE TABLE schema_version "
                "(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL, "
                "description TEXT)",
            )
            await db.execute(
                "INSERT INTO schema_version VALUES (19, 0.0, 'baseline')",
            )
            await db.execute("CREATE TABLE commitments (id TEXT PRIMARY KEY)")
            await db.execute("INSERT INTO commitments VALUES ('kept')")
            await db.commit()

            await _apply_migrations(db)

            assert set(await _column_info(db, "commitments")) == {"id"}
            async with db.execute("SELECT id FROM commitments") as cursor:
                assert [r[0] for r in await cursor.fetchall()] == ["kept"]
            async with db.execute(
                "SELECT COUNT(*) FROM schema_version WHERE version = 20",
            ) as cursor:
                row = await cursor.fetchone()
            assert row is not None
            assert row[0] == 1
        finally:
            await db.close()
