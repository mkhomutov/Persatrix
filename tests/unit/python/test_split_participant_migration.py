"""Tests for the [ISSUE-0120] split-person fold (memory migration v17).

The migration repairs what [ISSUE-0119] broke: a human whose group-channel
traffic landed on an ``agent``-typed relationship row beside the correct
``user``-typed row the DM path wrote.

What the suite pins, in the order the risk runs:

* the **selection rule** — only an id holding BOTH types is touched; a
  genuine agent peer (agent-typed row, no user-typed twin) is left byte-for-
  byte, and neither the ``principal`` nor the ``epoch`` wall is crossed;
* the **merge semantics** — summed counts, max timestamp, interaction-
  weighted trust, older-into-newer identity, the newer trust-change reason;
* **idempotency** — re-running cannot double the summed interaction count,
  which is the failure a crash-replay between the handler and the
  ``schema_version`` record would otherwise produce;
* the **guards** — a baseline without the table or without the v11/v12/v13
  columns returns cleanly instead of crashing the migration chain.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import aiosqlite
import pytest

from agents.memory._migration_split_participant import (
    _apply_migration_17,
    merge_trust,
)

# The v16 relationships shape this migration reads, mirroring the real
# post-chain table (verified against it). Built by hand rather than by running
# the whole chain so a schema change upstream surfaces here as an explicit
# edit rather than a silently-passing test.
#
# ``session_id`` is present but deliberately NOT in the primary key — the
# relationship row is a cross-session aggregate by design (RFC 0031 §C
# amendment), which is what makes the fold's self-join 1:1 rather than a
# cross product over one person's rooms. It is carried here so the fixture
# cannot drift into implying otherwise.
_SCHEMA = """
CREATE TABLE relationships (
    participant_id TEXT NOT NULL,
    participant_type TEXT NOT NULL DEFAULT 'agent',
    other_participant_id TEXT NOT NULL,
    other_participant_type TEXT NOT NULL DEFAULT 'agent',
    trust_score REAL DEFAULT 0.5,
    interaction_count INTEGER DEFAULT 0,
    last_interaction_at REAL,
    notes TEXT,
    session_id TEXT NOT NULL DEFAULT 'legacy',
    principal_id TEXT NOT NULL DEFAULT 'local',
    epoch_id TEXT NOT NULL DEFAULT 'live',
    identity TEXT,
    PRIMARY KEY (participant_id, participant_type,
                 other_participant_id, other_participant_type,
                 principal_id, epoch_id)
)
"""


async def _row(
    db: aiosqlite.Connection, other_id: str, other_type: str,
    *, principal: str = "local", epoch: str = "live",
) -> Sequence[Any] | None:
    cursor = await db.execute(
        """
        SELECT trust_score, interaction_count, last_interaction_at,
               notes, identity
        FROM relationships
        WHERE participant_id='ember-owl' AND other_participant_id=?
          AND other_participant_type=? AND principal_id=? AND epoch_id=?
        """,
        (other_id, other_type, principal, epoch),
    )
    return await cursor.fetchone()


async def _insert(
    db: aiosqlite.Connection, other_id: str, other_type: str, *,
    trust: float = 0.5, count: int = 0, last: float | None = None,
    notes: str | None = None, identity: dict | None = None,
    principal: str = "local", epoch: str = "live",
) -> None:
    await db.execute(
        """
        INSERT INTO relationships
            (participant_id, participant_type, other_participant_id,
             other_participant_type, trust_score, interaction_count,
             last_interaction_at, notes, identity, principal_id, epoch_id)
        VALUES ('ember-owl', 'agent', ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            other_id, other_type, trust, count, last, notes,
            json.dumps(identity) if identity is not None else None,
            principal, epoch,
        ),
    )


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    await conn.execute(_SCHEMA)
    await conn.commit()
    yield conn
    await conn.close()


# ─── Selection rule ─────────────────────────────────────────


class TestSelectionRule:
    async def test_agent_peer_without_a_user_twin_is_untouched(self, db):
        """The blast radius is exactly the bug's footprint.

        A real agent peer has only an agent-typed row. Rewriting it would
        invent a human where there is none — the inverse of the corruption
        this migration exists to repair.
        """
        await _insert(db, "agent-iron-fox", "agent", trust=0.9, count=7)
        await _apply_migration_17(db)

        row = await _row(db, "agent-iron-fox", "agent")
        assert row is not None, "an agent peer's row must survive the fold"
        assert row[0] == pytest.approx(0.9)
        assert row[1] == 7

    async def test_lone_user_row_is_untouched(self, db):
        """A human who never spoke in a group has nothing to merge."""
        await _insert(db, "alex", "user", trust=0.8, count=5)
        await _apply_migration_17(db)

        row = await _row(db, "alex", "user")
        assert row[0] == pytest.approx(0.8)
        assert row[1] == 5

    async def test_epoch_and_principal_walls_are_not_crossed(self, db):
        """Run-isolation and tenancy stay absolute.

        The user-typed row lives in one epoch/principal and the agent-typed
        row in another, so they are two different people as far as the
        scope axes are concerned — folding them would leak across exactly
        the walls docs/memory-scope-axes.md calls unconditional.
        """
        await _insert(db, "alex", "user", trust=0.8, count=5, epoch="live")
        await _insert(db, "alex", "agent", trust=0.2, count=3, epoch="test-run")
        await _insert(
            db, "sam", "user", trust=0.8, count=5, principal="tenant-a",
        )
        await _insert(
            db, "sam", "agent", trust=0.2, count=3, principal="tenant-b",
        )

        await _apply_migration_17(db)

        assert await _row(db, "alex", "agent", epoch="test-run") is not None
        assert await _row(db, "alex", "user", epoch="live") is not None
        assert await _row(db, "sam", "agent", principal="tenant-b") is not None
        assert await _row(db, "sam", "user", principal="tenant-a") is not None


# ─── Merge semantics ────────────────────────────────────────


class TestMergeSemantics:
    async def test_split_person_is_folded_onto_the_user_row(self, db):
        await _insert(
            db, "alex", "user", trust=0.72, count=40, last=1000.0,
            notes="steady collaboration", identity={"name": "Maksim"},
        )
        await _insert(
            db, "alex", "agent", trust=0.50, count=2, last=2000.0,
            notes="brief standup exchange",
            identity={"role": "maintainer", "prefs": ["Go"]},
        )

        await _apply_migration_17(db)

        assert await _row(db, "alex", "agent") is None, (
            "the agent-typed row is deleted — that is what makes the fold "
            "idempotent"
        )
        trust, count, last, notes, identity = await _row(db, "alex", "user")
        # (0.72*40 + 0.50*2) / 42
        assert trust == pytest.approx((0.72 * 40 + 0.50 * 2) / 42)
        assert count == 42, "counts sum — both rows recorded real interactions"
        assert last == pytest.approx(2000.0), "the later timestamp wins"
        assert notes == "brief standup exchange", (
            "notes holds the LATEST trust-change reason, so the newer row's "
            "value stands (concatenating would invent a reason)"
        )
        merged = json.loads(identity)
        assert merged == {
            "name": "Maksim", "role": "maintainer", "prefs": ["Go"],
        }, "identity unions across the fold; nothing learned is dropped"

    async def test_scalar_identity_conflict_resolves_to_the_newer_row(self, db):
        """Older-into-newer keeps the live write-through's last-writer-wins
        rule intact across the fold."""
        await _insert(
            db, "alex", "user", last=1000.0, identity={"role": "engineer"},
        )
        await _insert(
            db, "alex", "agent", last=2000.0, identity={"role": "maintainer"},
        )

        await _apply_migration_17(db)

        _, _, _, _, identity = await _row(db, "alex", "user")
        assert json.loads(identity)["role"] == "maintainer"

    async def test_user_row_wins_when_the_agent_row_never_interacted(self, db):
        await _insert(
            db, "alex", "user", last=1000.0, notes="from the DM",
            identity={"role": "engineer"},
        )
        await _insert(
            db, "alex", "agent", last=None, notes="never happened",
            identity={"role": "impostor"},
        )

        await _apply_migration_17(db)

        _, _, last, notes, identity = await _row(db, "alex", "user")
        assert last == pytest.approx(1000.0)
        assert notes == "from the DM"
        assert json.loads(identity)["role"] == "engineer"

    async def test_nulls_do_not_poison_the_merge(self, db):
        """The v4 columns are nullable; a legacy NULL must not produce NULL
        trust or a NULL count on the surviving row."""
        await db.execute(
            """
            INSERT INTO relationships
                (participant_id, participant_type, other_participant_id,
                 other_participant_type, trust_score, interaction_count,
                 principal_id, epoch_id)
            VALUES ('ember-owl','agent','alex','user',NULL,NULL,'local','live')
            """,
        )
        await _insert(db, "alex", "agent", trust=0.9, count=4)

        await _apply_migration_17(db)

        trust, count, *_ = await _row(db, "alex", "user")
        assert trust == pytest.approx(0.9), (
            "a NULL trust normalises to the 0.5 default and, with zero "
            "interactions of its own, contributes no weight"
        )
        assert count == 4


class TestMergeTrust:
    """The weighting rule on its own — pure, so the edge cases are cheap."""

    def test_weighted_by_interaction_count(self):
        assert merge_trust(0.72, 40, 0.50, 2) == pytest.approx(
            (0.72 * 40 + 0.50 * 2) / 42,
        )

    def test_high_count_row_dominates(self):
        """The whole point of weighting: 2 group interactions cannot outvote
        40 DM ones."""
        assert merge_trust(0.9, 40, 0.1, 2) > 0.85

    def test_no_interactions_keeps_the_user_value(self):
        assert merge_trust(0.8, 0, 0.2, 0) == pytest.approx(0.8)


# ─── Idempotency + guards ───────────────────────────────────


class TestIdempotencyAndGuards:
    async def test_rerun_does_not_double_the_counts(self, db):
        await _insert(db, "alex", "user", trust=0.7, count=40, last=1000.0)
        await _insert(db, "alex", "agent", trust=0.5, count=2, last=2000.0)

        await _apply_migration_17(db)
        first = await _row(db, "alex", "user")
        await _apply_migration_17(db)
        second = await _row(db, "alex", "user")

        assert first == second, (
            "a crash-replay between the handler and the schema_version "
            "record must be a clean no-op, not a second summing"
        )
        assert second[1] == 42

    async def test_missing_table_is_a_clean_noop(self):
        conn = await aiosqlite.connect(":memory:")
        try:
            await _apply_migration_17(conn)  # must not raise
        finally:
            await conn.close()

    async def test_missing_columns_is_a_clean_noop(self):
        """A pre-v11/v12/v13 baseline (no principal/epoch/identity) must not
        crash the chain — the later migrations have not run yet."""
        conn = await aiosqlite.connect(":memory:")
        try:
            await conn.execute(
                """
                CREATE TABLE relationships (
                    participant_id TEXT NOT NULL,
                    participant_type TEXT NOT NULL,
                    other_participant_id TEXT NOT NULL,
                    other_participant_type TEXT NOT NULL,
                    trust_score REAL, interaction_count INTEGER,
                    last_interaction_at REAL, notes TEXT
                )
                """,
            )
            await conn.commit()
            await _apply_migration_17(conn)  # must not raise
        finally:
            await conn.close()
