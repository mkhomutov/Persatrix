"""Tests for RFC 0021 Phase 2 PR 1 — memory-store migration v20, the
``commitments`` table.

One table holds two kinds of row (the Phase 2 plan's H-2): a persona's
own promise (``kind = 'commitment'``) and an instruction someone gave it
(``kind = 'directive'``, RFC 0012 §E's influence record).  Covers:

* a fresh store has the table, every column the plan names and the three
  indexes, and records v20
* the scope columns default exactly as the facts tier's do, so a row
  written without them is scoped as a fact written without them is
* each check constraint refuses a row of the wrong shape — and each
  refused row differs in one column from a row the table accepts, so the
  refusal can only come from the constraint named
* an in-place upgrade from a populated v19 store adds the table and
  leaves every earlier row and schema object as it was
* the handler is idempotent and leaves a pre-existing table alone

Nothing reads the table yet: PR 2's ``CommitmentStore`` is its first
reader.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import aiosqlite
import pytest

from agents.epoch_id import DEFAULT_EPOCH_ID
from agents.memory._migration_commitments import (
    COMMITMENT_STATES,
    DIRECTIVE_STATUSES,
    _apply_migration_20,
)
from agents.memory.episodic import EpisodicMemory
from agents.memory.migrations import (
    MIGRATIONS,
    PROTECTION_LEVEL_DEFAULT,
    _apply_migrations,
)
from agents.principal_id import DEFAULT_PRINCIPAL_ID
from agents.session_id import LEGACY_SESSION_ID

# A row of each kind the table accepts.  Every refusal case below starts
# from one of these and changes one column.
_COMMITMENT: dict[str, Any] = {
    "id": "c-1",
    "agent_id": "ember-owl",
    "kind": "commitment",
    "description": "send Bob the design notes",
    "due_at": 2_000.0,
    "target_party": "bob",
    "created_at": 1_000.0,
    "state": "open",
}
_DIRECTIVE: dict[str, Any] = {
    "id": "d-1",
    "agent_id": "ember-owl",
    "kind": "directive",
    "description": "do not post the launch date in #general",
    "created_at": 1_000.0,
    "issuer_id": "alice",
    "issuing_channel_id": "ops",
    "channel_authority": "operational",
    "issuer_authority": "directive",
    "scope": "general",
    "directive_status": "pending",
    "polarity": "do_not",
    "action_class": "publish_channel",
    "target": "general",
    "graph_version": 3,
}
_BASES = {"commitment": _COMMITMENT, "directive": _DIRECTIVE}

_SCOPE_COLUMNS = ("session_id", "principal_id", "epoch_id", "protection_level")


async def _insert(db: aiosqlite.Connection, row: dict[str, Any]) -> None:
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    await db.execute(
        f"INSERT INTO commitments ({cols}) VALUES ({marks})",  # noqa: S608 — test-owned keys
        tuple(row.values()),
    )


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
        cols = set(await _column_info(memory._ensure_db(), "commitments"))
        expected = {
            # RFC 0021 §F
            "id", "agent_id", "kind", "description", "due_at",
            "target_party", "created_at", "state", "fulfilled_at",
            "source_interaction_id", "tags_json",
            # RFC 0012 §E, and the RFC 0012 plan's asks of row A2
            "issuer_id", "issuing_channel_id", "channel_authority",
            "issuer_authority", "scope", "directive_status",
            "polarity", "action_class", "target", "graph_version",
            "conflicts_with_json", "conflict_checked_at",
            "conflict_failures",
            # the scope a fact carries
            *_SCOPE_COLUMNS,
        }
        assert cols == expected, (
            f"missing: {expected - cols}; unexpected: {cols - expected}"
        )

    @pytest.mark.parametrize(
        "index, columns",
        [
            # the open-commitments block: open rows, nearest due first
            ("idx_commitments_due", ["agent_id", "kind", "state", "due_at"]),
            # RFC 0012's reads, by directive status
            (
                "idx_commitments_directive",
                ["agent_id", "kind", "directive_status"],
            ),
            # ``list_commitments_to(target_party)``
            (
                "idx_commitments_target",
                ["agent_id", "target_party", "state"],
            ),
        ],
    )
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
                "AND target_party = 'bob'",
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
        await _insert(db, _COMMITMENT)
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
        await _insert(db, _DIRECTIVE)
        async with db.execute(
            "SELECT tags_json, conflicts_with_json, conflict_failures, "
            "conflict_checked_at FROM commitments WHERE id = 'd-1'",
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert tuple(row) == ("[]", "[]", 0, None)


# ─── Check constraints ──────────────────────────────────────


class TestCheckConstraints:
    async def test_well_formed_rows_accepted(self, memory: EpisodicMemory):
        """The positive controls every refusal below is measured from;
        a directive needs no ``due_at``."""
        db = memory._ensure_db()
        await _insert(db, _COMMITMENT)
        await _insert(db, _DIRECTIVE)
        await _insert(db, {**_DIRECTIVE, "id": "d-2", "due_at": 3_000.0})
        async with db.execute("SELECT COUNT(*) FROM commitments") as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] == 3

    @pytest.mark.parametrize("state", COMMITMENT_STATES)
    async def test_every_state_accepted(
        self, memory: EpisodicMemory, state: str,
    ):
        await _insert(memory._ensure_db(), {**_COMMITMENT, "state": state})

    @pytest.mark.parametrize("status", DIRECTIVE_STATUSES)
    async def test_every_directive_status_accepted(
        self, memory: EpisodicMemory, status: str,
    ):
        await _insert(
            memory._ensure_db(), {**_DIRECTIVE, "directive_status": status},
        )

    async def test_vocabularies_are_the_plans(self):
        assert COMMITMENT_STATES == ("open", "fulfilled", "missed", "cancelled")
        # The RFC 0012 plan's seven, outside the commitment ``state``
        # constraint.
        assert DIRECTIVE_STATUSES == (
            "pending", "accepted", "adapted", "refused", "escalated",
            "superseded", "fulfilled",
        )

    @pytest.mark.parametrize(
        "kind, change, constraint",
        [
            ("commitment", {"due_at": None}, "commitments_kind_shape"),
            ("commitment", {"state": None}, "commitments_kind_shape"),
            (
                "commitment",
                {"directive_status": "pending"},
                "commitments_kind_shape",
            ),
            ("commitment", {"issuer_id": "alice"}, "commitments_kind_shape"),
            ("directive", {"state": "open"}, "commitments_kind_shape"),
            ("directive", {"directive_status": None}, "commitments_kind_shape"),
            ("directive", {"issuer_id": None}, "commitments_kind_shape"),
            ("commitment", {"kind": "expectation"}, "commitments_kind"),
            ("commitment", {"state": "done"}, "commitments_state"),
            (
                "directive",
                {"directive_status": "approved"},
                "commitments_directive_status",
            ),
            (
                "directive",
                {"conflict_failures": -1},
                "commitments_conflict_failures",
            ),
        ],
        ids=[
            "commitment-without-due_at",
            "commitment-without-state",
            "commitment-with-directive_status",
            "commitment-with-issuer",
            "directive-with-state",
            "directive-without-directive_status",
            "directive-without-issuer",
            "unknown-kind",
            "unknown-state",
            "unknown-directive_status",
            "negative-conflict_failures",
        ],
    )
    async def test_wrong_shape_refused(
        self,
        memory: EpisodicMemory,
        kind: str,
        change: dict[str, Any],
        constraint: str,
    ):
        db = memory._ensure_db()
        with pytest.raises(
            sqlite3.IntegrityError,
            match=rf"CHECK constraint failed: {constraint}$",
        ):
            await _insert(db, {**_BASES[kind], **change})


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
    async def test_handler_replay_keeps_rows(self, memory: EpisodicMemory):
        """The crash between the DDL and the version record: running the
        handler again must neither raise nor rebuild the table."""
        db = memory._ensure_db()
        await _insert(db, _COMMITMENT)
        await db.commit()
        await _apply_migration_20(db)
        async with db.execute("SELECT id FROM commitments") as cursor:
            rows = [r[0] for r in await cursor.fetchall()]
        assert rows == ["c-1"]
        assert await _index_columns(db, "idx_commitments_due") == [
            "agent_id", "kind", "state", "due_at",
        ]

    async def test_umbrella_rerun_records_v20_once(
        self, memory: EpisodicMemory,
    ):
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
