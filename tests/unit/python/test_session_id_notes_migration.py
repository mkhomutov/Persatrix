"""
Tests for RFC 0031 Phase 2 PR 1 migration v9 — session_id on notes.

Phase 1 (migration v7) added ``session_id`` to ``episodes`` and
``relationships``; RFC 0026's migration v8 added it to ``facts``.  The
``notes`` tier (migration v2) was the remaining persona-memory recall
surface with no session dimension, which would re-introduce F-3 on the
notes prompt surface once the other tiers are filtered.  Migration v9
closes that gap with the column + per-table index, mirroring v7/v8's shape.

Covers:

* fresh-DB initialisation runs migration v9 and the new ``session_id``
  column + ``idx_notes_session`` index exist on ``notes``.
* in-place upgrade from a v8 baseline picks up the new column with the
  synthetic ``"legacy"`` default for every pre-existing note row.
* the v9 umbrella replay (schema_version row deleted to simulate
  crash-between-DDL-and-version-record) is idempotent — no duplicate
  column, no duplicate index.
* direct-handler replay is safe — invoking ``_apply_migration_9`` twice
  from a v8 baseline produces no duplicate column / index.
* the no-notes baseline (partial-restore shape) is a clean no-op for the
  handler — same contract as v7/v8.
* the umbrella records v9 even when the handler branch short-circuits.

Mirrors :mod:`tests.unit.python.test_session_id_migration` (the v7 pin
file) so a future refactor that drops the no-op guard from one but not the
other is caught.

PR 1 review fixes:

* F9 — duplicate-column assertion counts PRAGMA rows directly; the
  previous shape ``sum(1 for c in cols if c == "session_id") == 1`` ran
  over a ``set[str]`` and was trivially 0 or 1 regardless of the
  underlying DDL state.
* F10 — ``test_double_apply_is_noop`` now seeds a v8 baseline, runs the
  umbrella, DELETEs the v9 ``schema_version`` row to simulate
  crash-between-DDL-and-version-record, and runs the umbrella again;
  the previous shape used the fully-migrated ``memory`` fixture, so the
  umbrella's ``version > current`` filter short-circuited the second
  pass before ever reaching the v9 handler.
* F11 — ``test_direct_handler_replay_is_safe`` now starts from a v8
  baseline and invokes the handler twice in succession; the previous
  shape called the handler once on an already-migrated DB.
"""

from __future__ import annotations

import time as _time

import aiosqlite
import pytest

from agents.memory.episodic import EpisodicMemory
from agents.memory.migrations import (
    _MIGRATION_HANDLERS,
    MIGRATIONS,
    _apply_migration_9,
    _apply_migrations,
)


async def _columns(db: aiosqlite.Connection, table: str) -> set[str]:
    cursor = await db.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cursor.fetchall()}


async def _column_count(
    db: aiosqlite.Connection, table: str, name: str,
) -> int:
    """Count how many times ``name`` appears in ``PRAGMA table_info(table)``.

    Counts raw PRAGMA rows (not a set comprehension), so the result can
    actually witness a duplicate ``ADD COLUMN`` regression — the v9
    handler's ``if "session_id" not in existing`` guard is what we are
    pinning.  (PR 1 review F9 — the prior helper ``_columns`` returned a
    ``set[str]``, making any equality-filter count trivially 0 or 1.)
    """
    cursor = await db.execute(f"PRAGMA table_info({table})")
    return sum(1 for row in await cursor.fetchall() if row[1] == name)


async def _index_exists(db: aiosqlite.Connection, name: str) -> bool:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
        (name,),
    )
    return await cursor.fetchone() is not None


async def _seed_v8_baseline(db: aiosqlite.Connection) -> None:
    """Walk MIGRATIONS up to and including v8, recording each in
    ``schema_version``.  Leaves the DB at the schema state immediately
    before v9.  Mirrors the inline setup in
    :meth:`TestLegacyUpgrade.test_upgrade_from_v8_backfills_legacy` —
    extracted so the idempotency / replay tests can share one baseline
    rather than each rolling its own.
    """
    await db.execute(
        "CREATE TABLE IF NOT EXISTS schema_version "
        "(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL, "
        "description TEXT)",
    )
    for version, desc, sql in MIGRATIONS:
        if version > 8:
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
    async def test_migration_9_registered(self):
        versions = [v for v, _, _ in MIGRATIONS]
        assert 9 in versions

    async def test_notes_session_column_present(self, memory: EpisodicMemory):
        cols = await _columns(memory._ensure_db(), "notes")
        assert "session_id" in cols

    async def test_notes_session_index_created(self, memory: EpisodicMemory):
        assert await _index_exists(memory._ensure_db(), "idx_notes_session")

    async def test_schema_version_records_v9(self, memory: EpisodicMemory):
        async with memory._ensure_db().execute(
            "SELECT MAX(version) FROM schema_version",
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] >= 9


# ─── Idempotency ────────────────────────────────────────────


class TestMigrationIdempotency:
    async def test_umbrella_replay_after_version_record_loss_is_noop(self):
        """Umbrella replay must be idempotent after a crash between the v9
        DDL and the ``schema_version`` INSERT.

        Seeds a v8 baseline, runs the umbrella (brings the DB to v9),
        DELETEs the v9 row from ``schema_version`` to simulate the
        crash-between-DDL-and-version-record case, then runs the umbrella
        again.  The second pass must:

        * not raise on the already-present ``session_id`` column (the
          handler's ``PRAGMA table_info`` guard does this),
        * not produce a duplicate ``session_id`` column (counted via
          PRAGMA, not via a set),
        * not produce a duplicate ``idx_notes_session`` index,
        * record exactly one v9 row in ``schema_version`` at the end.

        (PR 1 review F10 — the previous shape called ``_apply_migrations``
        twice on the fully-migrated ``memory`` fixture, so the umbrella's
        ``version > current`` filter short-circuited the second pass
        before ever reaching the v9 handler.  That asserted the umbrella's
        version filter, not the handler's column-existence guard.)
        """
        db = await aiosqlite.connect(":memory:")
        try:
            await _seed_v8_baseline(db)

            # First umbrella pass — brings DB to v9, records the row.
            await _apply_migrations(db)
            assert await _column_count(db, "notes", "session_id") == 1
            assert await _index_exists(db, "idx_notes_session")

            # Simulate crash-between-DDL-and-version-record: drop the v9
            # row from schema_version while leaving the column + index in
            # place.  Also drop any later-version rows so the umbrella's
            # ``current = MAX(version)`` reads as 8 and re-dispatches the
            # v9 handler against an already-altered table — without this
            # the addition of v10 (RFC 0031 Phase 2 PR 5) would leave
            # ``current = 10`` and the v9 branch would be skipped.
            await db.execute("DELETE FROM schema_version WHERE version >= 9")
            await db.commit()

            # Second umbrella pass — handler re-runs against
            # already-altered notes table; PRAGMA guard skips ALTER,
            # CREATE INDEX IF NOT EXISTS skips the index.
            await _apply_migrations(db)

            assert await _column_count(db, "notes", "session_id") == 1
            assert await _index_exists(db, "idx_notes_session")
            async with db.execute(
                "SELECT COUNT(*) FROM schema_version WHERE version = 9",
            ) as cursor:
                row = await cursor.fetchone()
            assert row is not None
            assert row[0] == 1
        finally:
            await db.close()

    async def test_direct_handler_replay_is_safe(self):
        """Direct handler invocation must be idempotent across two
        consecutive calls from a v8 baseline.

        Starts at v8 (notes table created at v2, no session_id yet),
        invokes ``_apply_migration_9`` twice in succession, and asserts
        the column / index appear exactly once.  Pairs with the umbrella
        test above: this one pins the handler in isolation; that one
        pins the umbrella's dispatch + handler together.

        (PR 1 review F11 — the previous shape called the handler once on
        an already-v9-migrated DB, so the "replay" was a single call
        against an idempotent guard rather than two consecutive calls
        against a transitioning state.)
        """
        db = await aiosqlite.connect(":memory:")
        try:
            await _seed_v8_baseline(db)

            await _apply_migration_9(db)
            assert await _column_count(db, "notes", "session_id") == 1
            assert await _index_exists(db, "idx_notes_session")

            # Replay — must not raise, must not duplicate.
            await _apply_migration_9(db)
            assert await _column_count(db, "notes", "session_id") == 1
            assert await _index_exists(db, "idx_notes_session")
        finally:
            await db.close()


# ─── Empty-baseline guard ───────────────────────────────────


class TestEmptyTableGuard:
    """v9's handler must be a no-op when the notes table is missing.

    Mirrors the v5 / v6 / v7 contract: a partial-restore shape with
    ``schema_version`` recorded up to v8 but no ``notes`` table.
    ``ALTER TABLE`` on a nonexistent table would raise; the handler
    detects the missing table via ``sqlite_master`` and skips cleanly.
    """

    async def test_handler_no_op_on_missing_notes(self):
        db = await aiosqlite.connect(":memory:")
        try:
            await db.execute(
                "CREATE TABLE schema_version "
                "(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL, "
                "description TEXT)",
            )
            await db.execute(
                "INSERT INTO schema_version VALUES (8, 0.0, 'baseline')",
            )
            await db.commit()

            await _apply_migration_9(db)

            # Outer harness owns the version record — a direct handler
            # call must not touch ``schema_version``.
            cursor = await db.execute(
                "SELECT COUNT(*) FROM schema_version WHERE version = 9",
            )
            row = await cursor.fetchone()
            assert row[0] == 0

            # No notes table was present, so nothing was created.
            cursor = await db.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='notes'",
            )
            assert await cursor.fetchone() is None
        finally:
            await db.close()

    async def test_umbrella_records_v9_even_on_full_no_op(self):
        # Even when the notes table is missing, the umbrella records v9 as
        # applied — same contract as v5/v6/v7 so a later baseline rerun
        # cannot loop the upgrade.
        db = await aiosqlite.connect(":memory:")
        try:
            await db.execute(
                "CREATE TABLE schema_version "
                "(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL, "
                "description TEXT)",
            )
            await db.execute(
                "INSERT INTO schema_version VALUES (8, 0.0, 'v8 baseline')",
            )
            await db.commit()

            await _apply_migrations(db)

            cursor = await db.execute(
                "SELECT COUNT(*) FROM schema_version WHERE version = 9",
            )
            row = await cursor.fetchone()
            assert row[0] == 1
        finally:
            await db.close()


# ─── Legacy upgrade path ────────────────────────────────────


class TestLegacyUpgrade:
    async def test_upgrade_from_v8_backfills_legacy(self):
        """A DB pinned at v8 picks up v9 with ``'legacy'`` on existing rows."""
        db = await aiosqlite.connect(":memory:")
        try:
            await _seed_v8_baseline(db)

            # Insert a legacy note row shaped at the v8 schema (no
            # session_id column yet — migration v2 columns only).
            await db.execute(
                """
                INSERT INTO notes
                    (id, agent_id, topic, content, tags_json,
                     access_count, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    "legacy-note-1", "agent-x", "pre-session topic",
                    "pre-session content", "[]", 1000.0, 1000.0,
                ),
            )
            await db.commit()

            # Run the umbrella migration runner — picks up v9.
            await _apply_migrations(db)

            assert "session_id" in await _columns(db, "notes")

            async with db.execute(
                "SELECT session_id FROM notes WHERE id = ?",
                ("legacy-note-1",),
            ) as cursor:
                row = await cursor.fetchone()
            # Explicit None guard so a regression that drops the row
            # surfaces as AssertionError, not TypeError on row[0].
            assert row is not None, "legacy note row vanished during v9 migration"
            assert row[0] == "legacy"
        finally:
            await db.close()


# ─── Projection contract pin ────────────────────────────────


class TestNotesProjectionContract:
    """Pin the INSERT / SELECT / dataclass three-way contract.

    PR 1 (migration v9) widened the ``notes`` table to 9 columns
    (added ``session_id``) but deliberately kept ``_NOTE_COLS`` and the
    :class:`Note` dataclass at 8 fields so the recall API stayed
    unchanged.  PR 2 closes the gap: ``_NOTE_COLS`` now projects
    ``session_id`` and the :class:`Note` dataclass carries the field —
    moving the three together is exactly what this pin enforces.

    Without these pins, a one-sided edit (add a column to
    ``_NOTE_COLS`` without adjusting :meth:`NoteStore._row_to_note`,
    or vice versa) shifts the positional mapping and ``session_id``
    rolls into ``updated_at`` (or raises ``IndexError``) with no
    failing test to catch it.

    The :mod:`agents.memory.facts` module already carries the comment
    *"sync with SELECT statements — same pattern as _NOTE_COLS"* at
    :file:`facts.py:112`, showing maintainers already recognise this
    sync-hazard.  (PR 1 review F7.)
    """

    def test_note_cols_pinned_shape(self):
        """``_NOTE_COLS`` shape change MUST be a deliberate edit that
        also touches :meth:`NoteStore._row_to_note` and the :class:`Note`
        dataclass — this pin forces the author to re-confirm the
        positional contract by updating the literal here."""
        import dataclasses

        from agents.memory.notes import _NOTE_COLS, Note

        assert _NOTE_COLS == (
            "id", "agent_id", "topic", "content", "tags_json",
            "access_count", "created_at", "updated_at",
            "session_id",
            # RFC 0037 §C (migration v16, PR 4): the §D gate's projection.
            "protection_level", "source_channel_id",
        ), (
            "_NOTE_COLS shape change — also update Note dataclass + "
            "NoteStore._row_to_note positional mapping in "
            "agents/memory/note_types.py / notes.py"
        )
        # _row_to_note indexes row[0..len(_NOTE_COLS)-1] onto the Note
        # dataclass.  Field count must match the projection width so
        # positional hydration stays well-defined.
        assert len(dataclasses.fields(Note)) == len(_NOTE_COLS), (
            "Note dataclass field count drifted from _NOTE_COLS width — "
            "_row_to_note's positional indexing assumes 1:1 alignment"
        )

    async def test_row_to_note_round_trip_through_note_cols(
        self, memory: EpisodicMemory,
    ):
        """End-to-end: INSERT a note, SELECT it via the ``_NOTE_COLS``
        projection used by every recall path, hydrate via
        :meth:`NoteStore._row_to_note`, and confirm every dataclass
        field carries the value originally written.

        A one-sided edit (add a column to ``_NOTE_COLS`` without
        adjusting ``_row_to_note``, or vice versa) shifts the positional
        mapping and at least one field round-trips to a wrong value or
        raises ``IndexError`` — either way this test fails.
        """
        from agents.memory.notes import _NOTE_SELECT

        note_id = await memory.store_note(
            topic="pin-topic", content="pin-content", tags=["a", "b"],
        )
        async with memory._ensure_db().execute(
            f"SELECT {_NOTE_SELECT} FROM notes WHERE id = ?",
            (note_id,),
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None, "store_note INSERT silently dropped row"

        store = memory._ensure_note_store()
        note = store._row_to_note(row)

        assert note.id == note_id
        assert note.agent_id == "test-agent"  # memory fixture's agent_id
        assert note.topic == "pin-topic"
        assert note.content == "pin-content"
        assert note.tags == ["a", "b"]
        assert note.access_count == 0
        assert note.created_at > 0.0
        assert note.updated_at > 0.0
        # PR 2: the session_id projection lands on the dataclass.
        # ``memory`` fixture has no PERSATRIX_SESSION_ID set so the
        # store_note default falls through to ``"legacy"``.
        assert note.session_id == "legacy"
        # RFC 0037 §C (v16): the default stamp round-trips; notes carry
        # no per-channel provenance (see NoteStore.store_note).
        assert note.protection_level == "internal"
        assert note.source_channel_id is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
