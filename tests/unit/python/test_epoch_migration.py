"""ISSUE-0085 PR 2 migration v12 — ``epoch_id`` on all five tiers.

Migration v11 added the tenant dimension ``principal_id`` to ``episodes`` /
``relationships`` / ``facts`` / ``notes`` / ``interactions``; v12 adds the
orthogonal **run/test-isolation** dimension ``epoch_id`` to the same five
tiers at once (ISSUE-0085 — the structural half of the F-3 fix).

Where ``principal_id`` answers "which tenant owns this row?" with a
strict-equality recall predicate, ``epoch_id`` answers "which test run /
logical branch wrote this row?" with the *same* strict-equality predicate
— **no carve-out, no ``"*"`` sentinel** (a fresh epoch must see nothing).
The default is :data:`agents.epoch_id.DEFAULT_EPOCH_ID` (``'live'``) — the
epoch every production / untagged deployment uses; pre-existing rows
upgrade to it with no backfill UPDATE.

Mirrors :mod:`tests.unit.python.test_principal_migration`:

* fresh-DB initialisation runs v12 and every tier has the ``epoch_id``
  column + ``idx_<tier>_epoch`` index,
* the default value is ``'live'`` — pre-existing rows upgrade with no
  backfill,
* ``schema_version`` records v12,
* ``relationships`` is rebuilt with ``epoch_id`` *in the primary key*
  (alongside the ``principal_id`` v11 already put there), so an
  ``ON CONFLICT DO UPDATE`` under two epochs creates two rows rather than
  bleeding trust,
* umbrella replay (v12 row deleted) is idempotent,
* direct-handler replay from a v11 baseline is a clean no-op,
* the no-table partial-restore baseline does not crash the handler.
"""

from __future__ import annotations

import time as _time

import aiosqlite
import pytest

from agents.epoch_id import DEFAULT_EPOCH_ID
from agents.memory.episodic import EpisodicMemory
from agents.memory.migrations import (
    _MIGRATION_HANDLERS,
    MIGRATIONS,
    _apply_migration_12,
    _apply_migrations,
)
from agents.principal_id import DEFAULT_PRINCIPAL_ID

_TIERS = ("episodes", "relationships", "facts", "notes", "interactions")
_INDEXES = (
    "idx_episodes_epoch",
    "idx_rel_epoch",
    "idx_facts_epoch",
    "idx_notes_epoch",
    "idx_interactions_epoch",
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


async def _pk_columns(db: aiosqlite.Connection, table: str) -> set[str]:
    """Return the set of column names forming ``table``'s primary key."""
    cursor = await db.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cursor.fetchall() if row[5] > 0}


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


async def _seed_v11_baseline(db: aiosqlite.Connection) -> None:
    """Walk MIGRATIONS up to and including v11, recording each in
    ``schema_version`` — leaves the DB immediately before v12.
    """
    await db.execute(
        "CREATE TABLE IF NOT EXISTS schema_version "
        "(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL, "
        "description TEXT)",
    )
    for version, desc, sql in MIGRATIONS:
        if version > 11:
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
    async def test_migration_12_registered(self) -> None:
        versions = [v for v, _, _ in MIGRATIONS]
        assert 12 in versions

    @pytest.fixture
    async def memory(self):
        mem = EpisodicMemory(agent_id="t", db_path=":memory:")
        await mem.initialize()
        yield mem
        await mem.close()

    @pytest.mark.parametrize("table", _TIERS)
    async def test_epoch_column_present(
        self, memory: EpisodicMemory, table: str,
    ) -> None:
        assert memory._db is not None
        cols = await _columns(memory._db, table)
        assert "epoch_id" in cols, f"{table} missing epoch_id"

    @pytest.mark.parametrize("index_name", _INDEXES)
    async def test_epoch_index_created(
        self, memory: EpisodicMemory, index_name: str,
    ) -> None:
        assert memory._db is not None
        assert await _index_exists(memory._db, index_name)

    @pytest.mark.parametrize("table", _TIERS)
    async def test_epoch_default_is_live(
        self, memory: EpisodicMemory, table: str,
    ) -> None:
        assert memory._db is not None
        default = await _column_default(memory._db, table, "epoch_id")
        # PRAGMA reports the literal default including SQL quotes.
        assert default is not None
        assert DEFAULT_EPOCH_ID in default

    async def test_schema_version_records_v12(
        self, memory: EpisodicMemory,
    ) -> None:
        assert memory._db is not None
        async with memory._db.execute(
            "SELECT COUNT(*) FROM schema_version WHERE version = 12",
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] == 1


# ─── Relationships primary-key rebuild ──────────────────────


class TestRelationshipsPrimaryKey:
    """ISSUE-0085 — ``relationships`` needs ``epoch_id`` *in the primary
    key*, not merely as a column.

    Same reasoning v11 used for ``principal_id``: the aggregate
    ``relationships`` row is keyed on the participant tuple, so without the
    epoch axis in the key a rerun under a fresh epoch would
    ``ON CONFLICT DO UPDATE`` the prior run's row and the strict-equality
    recall filter would silently mask the trust bleed.  v12 rebuilds the
    table so each ``(participant tuple, principal, epoch)`` is a distinct
    row — ``principal_id`` (v11) stays in the key, ``epoch_id`` joins it.
    """

    @pytest.fixture
    async def memory(self):
        mem = EpisodicMemory(agent_id="t", db_path=":memory:")
        await mem.initialize()
        yield mem
        await mem.close()

    async def test_fresh_pk_includes_epoch_and_principal(
        self, memory: EpisodicMemory,
    ) -> None:
        assert memory._db is not None
        pk = await _pk_columns(memory._db, "relationships")
        assert pk == {
            "participant_id", "participant_type",
            "other_participant_id", "other_participant_type",
            "principal_id", "epoch_id",
        }

    async def test_v11_rebuild_preserves_row_and_keys_epoch(self) -> None:
        # Distinct, non-default sentinel values in *every* carried column so
        # a regression that drops a column from the rebuild's INSERT...SELECT
        # (or transposes two) fails loudly — the CREATE TABLE alone keeping a
        # column would otherwise hide a lost value behind its DEFAULT.  The
        # two ``*_type`` columns use 'user' (not the 'agent' default) for the
        # same reason.
        last_at = 12345.0
        async with aiosqlite.connect(":memory:") as db:
            await _seed_v11_baseline(db)
            # A pre-v12 relationships row (session_id + principal_id present,
            # no epoch_id).
            await db.execute(
                "INSERT INTO relationships "
                "(participant_id, participant_type, other_participant_id, "
                " other_participant_type, trust_score, interaction_count, "
                " last_interaction_at, notes, session_id, principal_id) "
                "VALUES ('a', 'user', 'peer', 'user', 0.9, 3, ?, 'n', "
                "        'run-a', 'tenant-x')",
                (last_at,),
            )
            await db.commit()

            await _apply_migration_12(db)

            # PK now carries the epoch axis alongside principal.
            pk = await _pk_columns(db, "relationships")
            assert "epoch_id" in pk
            assert "principal_id" in pk
            # Every pre-existing column round-trips through the rebuild and the
            # row is upgraded to the 'live' epoch (full-parity guard, not a
            # subset — see comment above).
            async with db.execute(
                "SELECT participant_id, participant_type, "
                "       other_participant_id, other_participant_type, "
                "       trust_score, interaction_count, last_interaction_at, "
                "       notes, session_id, principal_id, epoch_id "
                "FROM relationships WHERE participant_id='a' "
                "AND other_participant_id='peer'",
            ) as cur:
                row = await cur.fetchone()
            assert row is not None
            assert row[0] == "a"
            assert row[1] == "user"
            assert row[2] == "peer"
            assert row[3] == "user"
            assert row[4] == 0.9
            assert row[5] == 3
            assert row[6] == last_at
            assert row[7] == "n"
            assert row[8] == "run-a"
            assert row[9] == "tenant-x"
            assert row[10] == DEFAULT_EPOCH_ID

    async def test_two_epochs_coexist_as_distinct_rows(
        self, memory: EpisodicMemory,
    ) -> None:
        """The whole point of the rebuild: two epochs hold their own row for
        the same ``(participant tuple, principal)`` — an upsert under a fresh
        epoch cannot mutate the prior epoch's trust.
        """
        assert memory._db is not None
        db = memory._db
        for epoch in ("run-1", "run-2"):
            await db.execute(
                "INSERT INTO relationships "
                "(participant_id, participant_type, other_participant_id, "
                " other_participant_type, trust_score, principal_id, "
                " epoch_id) "
                "VALUES ('a', 'agent', 'peer', 'agent', 0.5, 'local', ?)",
                (epoch,),
            )
        await db.commit()
        async with db.execute(
            "SELECT COUNT(*) FROM relationships "
            "WHERE participant_id='a' AND other_participant_id='peer'",
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == 2

    async def test_principal_less_baseline_carries_default_principal(
        self,
    ) -> None:
        """Defensive branch: a ``relationships`` table without ``principal_id``
        upgrades cleanly, carrying the row forward under the *default*
        principal.

        The production ladder always runs v11 (which adds ``principal_id``)
        before v12, so this odd shape is unreachable in normal upgrades — but
        the handler guards against it (``has_principal_col`` carry-forward,
        mirroring v11's own).  Pinning it both covers that otherwise-untested
        branch and ties the fallback to :data:`DEFAULT_PRINCIPAL_ID` rather
        than a bare ``'local'`` literal, so the two move together if the
        constant ever changes.
        """
        async with aiosqlite.connect(":memory:") as db:
            # A v10-shape relationships table: participant tuple + session_id,
            # *no* principal_id, *no* epoch_id (i.e. before v11 ever ran).
            await db.execute(
                "CREATE TABLE relationships ("
                " participant_id TEXT NOT NULL,"
                " participant_type TEXT NOT NULL DEFAULT 'agent',"
                " other_participant_id TEXT NOT NULL,"
                " other_participant_type TEXT NOT NULL DEFAULT 'agent',"
                " trust_score REAL DEFAULT 0.5,"
                " interaction_count INTEGER DEFAULT 0,"
                " last_interaction_at REAL,"
                " notes TEXT,"
                " session_id TEXT NOT NULL DEFAULT 'legacy',"
                " PRIMARY KEY (participant_id, participant_type,"
                "              other_participant_id, other_participant_type))",
            )
            await db.execute(
                "INSERT INTO relationships "
                "(participant_id, participant_type, other_participant_id, "
                " other_participant_type, trust_score, session_id) "
                "VALUES ('a', 'agent', 'peer', 'agent', 0.7, 'run-a')",
            )
            await db.commit()

            await _apply_migration_12(db)

            pk = await _pk_columns(db, "relationships")
            assert "principal_id" in pk
            assert "epoch_id" in pk
            async with db.execute(
                "SELECT trust_score, session_id, principal_id, epoch_id "
                "FROM relationships WHERE participant_id='a' "
                "AND other_participant_id='peer'",
            ) as cur:
                row = await cur.fetchone()
            assert row is not None
            assert row[0] == 0.7
            assert row[1] == "run-a"
            assert row[2] == DEFAULT_PRINCIPAL_ID
            assert row[3] == DEFAULT_EPOCH_ID


# ─── In-place upgrade from a v11 baseline ───────────────────


class TestInPlaceUpgrade:
    async def test_v11_row_upgrades_to_live_epoch(self) -> None:
        async with aiosqlite.connect(":memory:") as db:
            await _seed_v11_baseline(db)
            # A pre-v12 episode row (no epoch_id column yet).
            await db.execute(
                "INSERT INTO episodes "
                "(id, agent_id, summary, created_at, session_id, principal_id) "
                "VALUES ('e1', 'a', 's', ?, 'run-a', 'local')",
                (_time.time(),),
            )
            await db.commit()

            await _apply_migration_12(db)

            cols = await _columns(db, "episodes")
            assert "epoch_id" in cols
            async with db.execute(
                "SELECT epoch_id FROM episodes WHERE id='e1'",
            ) as cur:
                row = await cur.fetchone()
            assert row is not None
            assert row[0] == DEFAULT_EPOCH_ID


# ─── Idempotency ────────────────────────────────────────────


class TestMigrationIdempotency:
    async def test_umbrella_replay_is_idempotent(self) -> None:
        """Deleting the v12 schema_version row (crash-between-DDL-and-record
        shape) and re-running _apply_migrations adds no duplicate column.
        """
        async with aiosqlite.connect(":memory:") as db:
            await _apply_migrations(db)
            await db.execute("DELETE FROM schema_version WHERE version=12")
            await db.commit()

            await _apply_migrations(db)

            for table in _TIERS:
                assert await _column_count(db, table, "epoch_id") == 1

    async def test_direct_handler_replay_from_v11(self) -> None:
        async with aiosqlite.connect(":memory:") as db:
            await _seed_v11_baseline(db)
            await _apply_migration_12(db)
            # Second invocation must be a clean no-op.
            await _apply_migration_12(db)
            for table in _TIERS:
                assert await _column_count(db, table, "epoch_id") == 1
                assert "epoch_id" in await _columns(db, table)


# ─── Partial-restore baseline ───────────────────────────────


class TestPartialRestoreBaseline:
    async def test_no_tables_is_clean_noop(self) -> None:
        """A baseline with schema_version recorded but no tier tables
        (partial restore) must not crash the ADD COLUMN — same contract
        as v7/v9/v10/v11.
        """
        async with aiosqlite.connect(":memory:") as db:
            await db.execute(
                "CREATE TABLE schema_version "
                "(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL, "
                "description TEXT)",
            )
            await db.commit()
            # Must not raise even though none of the five tables exist.
            await _apply_migration_12(db)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
