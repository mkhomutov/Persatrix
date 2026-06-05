"""RFC 0031 amendment (F-7 Option D, ISSUE-0093) PR D1 — person identity on
the cross-room relationship tier.

This PR is the **storage + write-API foundation** only — no prompt-facing
behavior change yet (rendering is D2, retirement of the Option-A
``contact:*`` recall carve-out is D3).  It adds:

* migration **v13**: a nullable ``identity TEXT`` (JSON) column on
  ``relationships`` — additive, no primary-key rebuild (identity is not a
  key column), so it follows the simple v7 ``ADD COLUMN`` skeleton rather
  than the v11/v12 table rebuild.
* :func:`agents.memory.relationship_types.merge_identity` — the pure
  shallow-merge (scalar last-writer-wins; ``prefs`` order-preserving
  union) that gives identity deterministic supersede semantics (decision
  D-2 in the amendment).
* :meth:`RelationshipMemory.upsert_identity` — a non-destructive merge
  write that lands on the relationship row (PK excludes ``session_id`` →
  cross-room by construction) and **never touches the trust ``notes``
  column** (decision D-2: identity gets a dedicated column so a trust-
  reason write cannot clobber a name).
* :meth:`RelationshipMemory.get_identity` — a read that applies
  principal/epoch strict equality **but no session filter**, so identity
  stated in room A surfaces in room B (the cross-room property that makes
  the F-7 seam impossible by construction — there is no ``sessions="*"``
  sentinel; the room axis simply is not part of the tier's key).
"""

from __future__ import annotations

import os
import tempfile

import aiosqlite
import pytest

from agents.epoch_id import EPOCH_ID_ENV_VAR
from agents.memory.migrations import (
    MIGRATIONS,
    _apply_migrations,
)
from agents.memory.relationship import RelationshipMemory
from agents.memory.relationship_types import merge_identity
from agents.principal_id import PRINCIPAL_ID_ENV_VAR
from agents.session_id import SESSION_ID_ENV_VAR

# ─── Fixtures ───────────────────────────────────────────────


@pytest.fixture
async def memory():
    """An initialized in-memory ``RelationshipMemory``."""
    mem = RelationshipMemory(agent_id="test-agent", db_path=":memory:")
    await mem.initialize()
    yield mem
    await mem.close()


@pytest.fixture
def db_path():
    """A throwaway on-disk DB path (shared across two store instances)."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        yield path
    finally:
        os.unlink(path)


async def _columns(db: aiosqlite.Connection, table: str) -> set[str]:
    cursor = await db.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cursor.fetchall()}


# ─── merge_identity (pure) ──────────────────────────────────


class TestMergeIdentity:
    def test_merge_into_empty_returns_incoming(self):
        assert merge_identity({}, {"name": "Max"}) == {"name": "Max"}

    def test_scalar_last_writer_wins(self):
        merged = merge_identity({"name": "Max"}, {"name": "Maxim"})
        assert merged == {"name": "Maxim"}

    def test_new_key_is_added(self):
        merged = merge_identity({"name": "Max"}, {"role": "engineer"})
        assert merged == {"name": "Max", "role": "engineer"}

    def test_prefs_union_order_preserving_dedup(self):
        merged = merge_identity(
            {"prefs": ["Rust"]}, {"prefs": ["Rust", "Go"]},
        )
        assert merged == {"prefs": ["Rust", "Go"]}

    def test_prefs_scalar_coerced_to_list(self):
        merged = merge_identity({"prefs": ["Rust"]}, {"prefs": "Go"})
        assert merged == {"prefs": ["Rust", "Go"]}

    def test_none_value_does_not_clobber(self):
        merged = merge_identity({"name": "Max"}, {"name": None})
        assert merged == {"name": "Max"}

    def test_does_not_mutate_inputs(self):
        existing = {"name": "Max", "prefs": ["Rust"]}
        incoming = {"prefs": ["Go"]}
        merge_identity(existing, incoming)
        assert existing == {"name": "Max", "prefs": ["Rust"]}
        assert incoming == {"prefs": ["Go"]}


# ─── Migration v13 ──────────────────────────────────────────


class TestIdentityMigration:
    async def test_fresh_db_has_identity_column(self, memory):
        cols = await _columns(memory._ensure_db(), "relationships")
        assert "identity" in cols

    async def test_identity_defaults_null(self, memory):
        """A row created without identity (e.g. via trust) has NULL identity."""
        await memory.update_trust("peer", 0.1, "worked well")
        async with memory._ensure_db().execute(
            "SELECT identity FROM relationships "
            "WHERE other_participant_id = 'peer'",
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] is None

    async def test_schema_version_records_v13(self, memory):
        async with memory._ensure_db().execute(
            "SELECT MAX(version) FROM schema_version",
        ) as cursor:
            row = await cursor.fetchone()
        assert row[0] >= 13

    async def test_migration_registered(self):
        versions = {v for v, _desc, _sql in MIGRATIONS}
        assert 13 in versions

    async def test_umbrella_replay_idempotent(self, db_path):
        """Re-running _apply_migrations after dropping the v13 row is a no-op."""
        db = await aiosqlite.connect(db_path)
        try:
            await _apply_migrations(db)
            await db.execute("DELETE FROM schema_version WHERE version = 13")
            await db.commit()
            # Replay must not crash on the already-present identity column.
            await _apply_migrations(db)
            cols = await _columns(db, "relationships")
            assert "identity" in cols
        finally:
            await db.close()


# ─── upsert_identity / get_identity ─────────────────────────


class TestUpsertIdentity:
    async def test_upsert_creates_row_and_get_returns_identity(self, memory):
        await memory.upsert_identity("local", {"name": "Max"})
        assert await memory.get_identity("local") == {"name": "Max"}

    async def test_get_identity_none_when_absent(self, memory):
        assert await memory.get_identity("nobody") is None

    async def test_upsert_does_not_set_trust_reason(self, memory):
        """Creating a row via identity leaves notes (trust reason) NULL."""
        await memory.upsert_identity("local", {"name": "Max"})
        async with memory._ensure_db().execute(
            "SELECT notes, trust_score FROM relationships "
            "WHERE other_participant_id = 'local'",
        ) as cursor:
            notes, trust = await cursor.fetchone()
        assert notes is None
        assert trust == pytest.approx(0.5)

    async def test_second_upsert_merges(self, memory):
        await memory.upsert_identity(
            "local", {"name": "Max", "prefs": ["Rust"]},
        )
        await memory.upsert_identity(
            "local", {"name": "Maxim", "prefs": ["Go"]},
        )
        assert await memory.get_identity("local") == {
            "name": "Maxim",
            "prefs": ["Rust", "Go"],
        }

    async def test_identity_and_trust_do_not_clobber_each_other(self, memory):
        """Identity write + trust write coexist on the same row."""
        await memory.upsert_identity("local", {"name": "Max"})
        await memory.update_trust("local", 0.2, "helpful in standup")
        # Trust reason landed; identity survived.
        assert await memory.get_identity("local") == {"name": "Max"}
        summary = await memory.get_relationship_summary("local")
        assert summary.notes == "helpful in standup"
        # Now another identity write must not wipe the trust reason.
        await memory.upsert_identity("local", {"role": "engineer"})
        summary = await memory.get_relationship_summary("local")
        assert summary.notes == "helpful in standup"
        assert await memory.get_identity("local") == {
            "name": "Max", "role": "engineer",
        }


# ─── Cross-room + isolation boundaries ──────────────────────


class TestIdentityScope:
    async def test_identity_crosses_rooms(
        self, db_path, monkeypatch: pytest.MonkeyPatch,
    ):
        """Identity stated in session/room A is recalled from room B.

        The relationship PK excludes ``session_id``; ``get_identity``
        applies no session filter — so the single ``(pair, principal,
        epoch)`` row's identity surfaces regardless of the active room.
        This is the F-7 seam closed by construction.
        """
        monkeypatch.setenv(SESSION_ID_ENV_VAR, "room-a")
        mem_a = RelationshipMemory(agent_id="agent", db_path=db_path)
        await mem_a.initialize()
        await mem_a.upsert_identity("local", {"name": "Max"})
        await mem_a.close()

        monkeypatch.setenv(SESSION_ID_ENV_VAR, "room-b")
        mem_b = RelationshipMemory(agent_id="agent", db_path=db_path)
        await mem_b.initialize()
        try:
            assert await mem_b.get_identity("local") == {"name": "Max"}
        finally:
            await mem_b.close()

    async def test_identity_isolated_across_principals(
        self, db_path, monkeypatch: pytest.MonkeyPatch,
    ):
        """A tenant boundary holds — cross-room is never cross-tenant."""
        monkeypatch.setenv(PRINCIPAL_ID_ENV_VAR, "tenant-x")
        mem_x = RelationshipMemory(agent_id="agent", db_path=db_path)
        await mem_x.initialize()
        await mem_x.upsert_identity("local", {"name": "Max"})
        await mem_x.close()

        monkeypatch.setenv(PRINCIPAL_ID_ENV_VAR, "tenant-y")
        mem_y = RelationshipMemory(agent_id="agent", db_path=db_path)
        await mem_y.initialize()
        try:
            assert await mem_y.get_identity("local") is None
        finally:
            await mem_y.close()

    async def test_identity_isolated_across_epochs(
        self, db_path, monkeypatch: pytest.MonkeyPatch,
    ):
        """A run/epoch boundary holds — cross-room is never cross-epoch."""
        monkeypatch.setenv(EPOCH_ID_ENV_VAR, "epoch-1")
        mem_1 = RelationshipMemory(agent_id="agent", db_path=db_path)
        await mem_1.initialize()
        await mem_1.upsert_identity("local", {"name": "Max"})
        await mem_1.close()

        monkeypatch.setenv(EPOCH_ID_ENV_VAR, "epoch-2")
        mem_2 = RelationshipMemory(agent_id="agent", db_path=db_path)
        await mem_2.initialize()
        try:
            assert await mem_2.get_identity("local") is None
        finally:
            await mem_2.close()
