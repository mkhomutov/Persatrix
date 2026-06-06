"""RFC 0031 amendment (F-7 Option D, ISSUE-0093) PR D4 — one-time backfill
of pre-cutover ``contact:*`` notes onto relationship identity.

D1–D3 moved person identity (name / role / stable preferences) onto the
cross-room ``relationships`` tier and retired the F-7 Option-A
``contact:*`` recall carve-out.  But identity *captured before the
cutover* still lives only in room-scoped ``contact:<id>`` notes — invisible
to the relationship-tier render.  Migration **v14** reads those legacy
notes and ``upsert_identity``-merges them onto the matching relationship
row so personas don't lose what they learned pre-cutover.

**The participant-type design choice (the reason this is its own PR).**  A
``contact:<id>`` note records ``agent_id`` (the persona = ``participant_id``,
always ``participant_type='agent'``), ``principal_id``, ``epoch_id`` and —
from the topic — ``other_participant_id``.  The *only* relationship-PK axis
it does **not** record is ``other_participant_type``.  The backfill
resolves it without guessing the other three (tenant/epoch isolation is
never fabricated):

* **Inherit** — merge identity onto *every* existing relationship row for
  the recorded ``(agent_id, principal_id, epoch_id, other_id)`` tuple,
  whatever ``other_participant_type`` those rows carry.  Identity lands
  exactly on the row(s) recall anchors on; no type is invented.
* **Orphan fallback** — when no relationship row exists for the tuple,
  create one under ``normalize_sender_type(None)`` (``"agent"`` — the same
  default both the write-through and the recall side fall back to when the
  sender type is unbound), at neutral trust, interaction-free.  This
  preserves the identity (the backfill's whole purpose) without
  fabricating a tenant/epoch axis.

These tests pin both legs plus the non-destructive / non-clobber / scope /
idempotency contracts.
"""

from __future__ import annotations

import json
import os
import tempfile

import aiosqlite
import pytest

from agents.memory._migration_identity_backfill import _apply_migration_14
from agents.memory.migrations import _apply_migrations
from agents.memory.relationship_types import _DEFAULT_TRUST

# ─── Fixtures / helpers ─────────────────────────────────────


@pytest.fixture
async def db():
    """A fully-migrated throwaway on-disk DB (schema at the latest version).

    On-disk (not ``:memory:``) so the connection can be reasoned about as a
    single shared handle; migrations create every tier's table, so both
    ``notes`` and ``relationships`` exist.
    """
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = await aiosqlite.connect(path)
    try:
        await _apply_migrations(conn)
        yield conn
    finally:
        await conn.close()
        os.unlink(path)


async def _insert_note(
    conn: aiosqlite.Connection,
    *,
    agent_id: str,
    topic: str,
    content: str,
    principal_id: str = "local",
    epoch_id: str = "live",
    session_id: str = "legacy",
    created_at: float = 1000.0,
    note_id: str | None = None,
) -> None:
    """Seed a row shaped like a pre-cutover ``store_note`` write."""
    await conn.execute(
        """
        INSERT INTO notes
            (id, agent_id, topic, content, tags_json, access_count,
             created_at, updated_at, session_id, principal_id, epoch_id)
        VALUES (?, ?, ?, ?, NULL, 0, ?, ?, ?, ?, ?)
        """,
        (
            note_id or f"note-{topic}-{created_at}",
            agent_id, topic, content, created_at, created_at,
            session_id, principal_id, epoch_id,
        ),
    )
    await conn.commit()


async def _insert_relationship(
    conn: aiosqlite.Connection,
    *,
    agent_id: str,
    other_id: str,
    other_participant_type: str = "user",
    trust_score: float = 0.7,
    interaction_count: int = 3,
    notes: str | None = "helped with a deploy",
    identity: str | None = None,
    principal_id: str = "local",
    epoch_id: str = "live",
    session_id: str = "room-a",
) -> None:
    """Seed an existing relationship row (e.g. from ``record_interaction``)."""
    await conn.execute(
        """
        INSERT INTO relationships
            (participant_id, participant_type,
             other_participant_id, other_participant_type,
             trust_score, interaction_count, last_interaction_at,
             notes, identity, session_id, principal_id, epoch_id)
        VALUES (?, 'agent', ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)
        """,
        (
            agent_id, other_id, other_participant_type,
            trust_score, interaction_count, notes, identity,
            session_id, principal_id, epoch_id,
        ),
    )
    await conn.commit()


async def _identity_rows(
    conn: aiosqlite.Connection, agent_id: str, other_id: str,
) -> list[tuple[str, dict | None, float, int, str | None]]:
    """Return ``(other_type, identity_dict, trust, count, notes)`` per row."""
    cursor = await conn.execute(
        """
        SELECT other_participant_type, identity, trust_score,
               interaction_count, notes
        FROM relationships
        WHERE participant_id=? AND participant_type='agent'
          AND other_participant_id=?
        ORDER BY other_participant_type
        """,
        (agent_id, other_id),
    )
    out = []
    for other_type, identity_json, trust, count, notes in await cursor.fetchall():
        out.append((
            other_type,
            json.loads(identity_json) if identity_json else None,
            trust, count, notes,
        ))
    return out


# ─── Inherit: land on the existing row's type ───────────────


class TestBackfillInherit:
    async def test_merges_onto_existing_row_inheriting_type(self, db):
        """A contact note lands on the existing relationship row, keeping its
        ``other_participant_type`` — the type is inherited, never guessed."""
        await _insert_relationship(
            db, agent_id="p", other_id="alice", other_participant_type="user",
            identity=None,
        )
        await _insert_note(
            db, agent_id="p", topic="contact:alice",
            content="Name: Alice. Role: engineer. Favorite language: Rust.",
        )

        await _apply_migration_14(db)

        rows = await _identity_rows(db, "p", "alice")
        assert len(rows) == 1
        other_type, identity, _, _, _ = rows[0]
        assert other_type == "user"
        assert identity == {
            "name": "Alice", "role": "engineer", "prefs": ["Rust"],
        }

    async def test_multiple_notes_merge_chronologically(self, db):
        """Several notes about one contact combine; a later ``name``
        supersedes an earlier one and ``prefs`` union (created_at order)."""
        await _insert_relationship(
            db, agent_id="p", other_id="bob", other_participant_type="user",
        )
        await _insert_note(
            db, agent_id="p", topic="contact:bob", created_at=100.0,
            content="Name: Bob. Favorite language: Go.",
        )
        await _insert_note(
            db, agent_id="p", topic="contact:bob", created_at=200.0,
            content="Name: Bobby. Favorite language: Rust.",
        )

        await _apply_migration_14(db)

        _, identity, _, _, _ = (await _identity_rows(db, "p", "bob"))[0]
        assert identity["name"] == "Bobby"  # later note wins
        assert identity["prefs"] == ["Go", "Rust"]  # union, chronological

    async def test_does_not_clobber_existing_identity(self, db):
        """A pre-existing ``identity`` (e.g. from a post-cutover write-through)
        is merged into, not overwritten — existing prefs are retained."""
        await _insert_relationship(
            db, agent_id="p", other_id="cara", other_participant_type="user",
            identity=json.dumps({"name": "Cara", "prefs": ["tea"]}),
        )
        await _insert_note(
            db, agent_id="p", topic="contact:cara",
            content="Favorite drink: coffee.",
        )

        await _apply_migration_14(db)

        _, identity, _, _, _ = (await _identity_rows(db, "p", "cara"))[0]
        assert identity["name"] == "Cara"
        assert identity["prefs"] == ["tea", "coffee"]

    async def test_never_touches_trust_or_notes(self, db):
        """Backfill writes only ``identity`` — trust_score / interaction_count
        / the trust-reason ``notes`` column are left exactly as they were."""
        await _insert_relationship(
            db, agent_id="p", other_id="dan", other_participant_type="user",
            trust_score=0.83, interaction_count=5, notes="resolved an incident",
        )
        await _insert_note(
            db, agent_id="p", topic="contact:dan", content="Name: Dan.",
        )

        await _apply_migration_14(db)

        other_type, identity, trust, count, notes = (
            await _identity_rows(db, "p", "dan"))[0]
        assert identity == {"name": "Dan"}
        assert trust == pytest.approx(0.83)
        assert count == 5
        assert notes == "resolved an incident"


# ─── Orphan fallback: default type, identity preserved ──────


class TestBackfillOrphan:
    async def test_orphan_note_creates_default_agent_row(self, db):
        """A contact note with no matching relationship row creates one under
        the default ``other_participant_type='agent'`` at neutral trust,
        interaction-free — identity is preserved, not lost."""
        await _insert_note(
            db, agent_id="p", topic="contact:eve",
            content="Name: Eve. Role: designer.",
        )

        await _apply_migration_14(db)

        rows = await _identity_rows(db, "p", "eve")
        assert len(rows) == 1
        other_type, identity, trust, count, notes = rows[0]
        assert other_type == "agent"  # normalize_sender_type(None)
        assert identity == {"name": "Eve", "role": "designer"}
        assert trust == pytest.approx(_DEFAULT_TRUST)
        assert count == 0
        assert notes is None

    async def test_orphan_respects_recorded_principal_epoch(self, db):
        """The orphan row is created under the note's recorded principal /
        epoch — never a fabricated tenant/epoch axis."""
        await _insert_note(
            db, agent_id="p", topic="contact:frank",
            content="Name: Frank.", principal_id="tenant-x", epoch_id="run-7",
        )

        await _apply_migration_14(db)

        cursor = await db.execute(
            "SELECT principal_id, epoch_id FROM relationships "
            "WHERE other_participant_id='frank'",
        )
        assert await cursor.fetchall() == [("tenant-x", "run-7")]


# ─── Scope / selectivity ────────────────────────────────────


class TestBackfillScope:
    async def test_principal_isolation(self, db):
        """A note under one principal does not bleed onto a relationship row
        under a different principal — only the matching tuple is updated."""
        await _insert_relationship(
            db, agent_id="p", other_id="gina", principal_id="A",
        )
        await _insert_relationship(
            db, agent_id="p", other_id="gina", principal_id="B",
        )
        await _insert_note(
            db, agent_id="p", topic="contact:gina", content="Name: Gina.",
            principal_id="A",
        )

        await _apply_migration_14(db)

        cursor = await db.execute(
            "SELECT principal_id, identity FROM relationships "
            "WHERE other_participant_id='gina' ORDER BY principal_id",
        )
        rows = await cursor.fetchall()
        assert rows[0][0] == "A" and json.loads(rows[0][1]) == {"name": "Gina"}
        assert rows[1][0] == "B" and rows[1][1] is None  # untouched

    async def test_non_contact_notes_ignored(self, db):
        """A room/topic note (not ``contact:*``) is never read into identity."""
        await _insert_relationship(db, agent_id="p", other_id="henry")
        await _insert_note(
            db, agent_id="p", topic="Persatrix",
            content="Name: Henry.",  # name-shaped, but not a contact note
        )

        await _apply_migration_14(db)

        _, identity, _, _, _ = (await _identity_rows(db, "p", "henry"))[0]
        assert identity is None

    async def test_unparseable_contact_note_is_skipped(self, db):
        """A contact note whose content yields no structured field creates no
        row and changes nothing (no phantom identity)."""
        await _insert_relationship(db, agent_id="p", other_id="ivy")
        await _insert_note(
            db, agent_id="p", topic="contact:ivy", content="   ...   ",
        )

        await _apply_migration_14(db)

        _, identity, _, _, _ = (await _identity_rows(db, "p", "ivy"))[0]
        assert identity is None

    async def test_blank_other_id_is_skipped(self, db):
        """A degenerate ``contact:`` topic (no id) is skipped, not crashed."""
        await _insert_note(
            db, agent_id="p", topic="contact:", content="Name: Nobody.",
        )

        await _apply_migration_14(db)

        cursor = await db.execute("SELECT COUNT(*) FROM relationships")
        assert (await cursor.fetchone())[0] == 0


# ─── Idempotency / empty ────────────────────────────────────


class TestBackfillIdempotency:
    async def test_running_twice_is_a_no_op(self, db):
        """Re-running the backfill (crash-replay) does not duplicate rows or
        pollute ``prefs`` — the merge is idempotent."""
        await _insert_relationship(
            db, agent_id="p", other_id="jack", other_participant_type="user",
        )
        await _insert_note(
            db, agent_id="p", topic="contact:jack",
            content="Name: Jack. Favorite language: Rust.",
        )

        await _apply_migration_14(db)
        first = await _identity_rows(db, "p", "jack")
        await _apply_migration_14(db)
        second = await _identity_rows(db, "p", "jack")

        assert first == second
        assert len(second) == 1
        assert second[0][1] == {"name": "Jack", "prefs": ["Rust"]}

    async def test_empty_notes_table_is_a_no_op(self, db):
        """No contact notes → nothing created, no error."""
        await _apply_migration_14(db)
        cursor = await db.execute("SELECT COUNT(*) FROM relationships")
        assert (await cursor.fetchone())[0] == 0
