"""ISSUE-0081 PR 3 migration v11 — ``principal_id`` on every memory tier.

Split out of :mod:`agents.memory._migration_handlers` to keep that module
under the project's 500-line review-friendly cap (see
``scripts/checks/file_size.py``), mirroring the v8 / v9 / v10 splits.

Where the ``session_id`` column (migrations v7 / v8 / v9 / v10) answers
"which operator run wrote this row?", ``principal_id`` answers "which
tenant / authenticated human owns it?" (RFC 0031 §C amendment).  This
migration adds the column to **all five** persona-memory tables in one
version so the tenant dimension lands atomically across tiers —
``episodes`` / ``relationships`` / ``facts`` / ``notes`` /
``interactions``.

The ``DEFAULT 'local'`` value is :data:`agents.principal_id.DEFAULT_PRINCIPAL_ID`
— the single-tenant principal every unauthenticated deployment uses.
Pre-existing rows upgrade to it with no backfill UPDATE.  Unlike the
``'legacy'`` session default this is **not** a cross-tenant carve-out:
the recall predicate (:func:`agents.memory._principal_filter.principal_eq_clause`)
is strict equality, so once a second tenant exists, ``'local'`` rows are
visible only to the ``'local'`` principal.

Idempotency / partial-restore safety: identical skeleton to v7 / v9 /
v10 — ``ALTER TABLE ... ADD COLUMN`` predates ``IF NOT EXISTS`` in
SQLite < 3.35, so each table is guarded by a ``sqlite_master`` existence
check (short-circuits a partial-restore baseline whose ``schema_version``
is recorded past v11 but lacks a given table) and a ``PRAGMA table_info``
column check before the ``ADD COLUMN``.  The per-table ``CREATE INDEX IF
NOT EXISTS`` is the standalone session-index analogue: it keeps the tiers
uniform and gives a future principal-scoped maintenance op an index
without a follow-up migration; it is not the index recall seeks (the
recall queries are anchored on ``agent_id`` / the participant tuple and
apply ``principal_id`` as a residual filter).
"""

from __future__ import annotations

import aiosqlite

from ..principal_id import DEFAULT_PRINCIPAL_ID

#: ``(table, principal-index name)`` pairs.  All five persona-memory
#: tables gain the column in one migration.  Both members of each pair
#: are trusted internal literals — never user input — so the f-string
#: interpolation below is safe (same contract as the v7 handler).
_PRINCIPAL_TABLES: tuple[tuple[str, str], ...] = (
    ("episodes", "idx_episodes_principal"),
    ("relationships", "idx_rel_principal"),
    ("facts", "idx_facts_principal"),
    ("notes", "idx_notes_principal"),
    ("interactions", "idx_interactions_principal"),
)


async def _apply_migration_11(db: aiosqlite.Connection) -> None:
    """ISSUE-0081 PR 3: ``principal_id`` on all five persona-memory tiers.

    Adds ``principal_id TEXT NOT NULL DEFAULT '<DEFAULT_PRINCIPAL_ID>'``
    and an ``idx_<tier>_principal`` index to each of ``episodes`` /
    ``relationships`` / ``facts`` / ``notes`` / ``interactions``.

    Each table is handled independently with the v7 ``sqlite_master`` +
    ``PRAGMA table_info`` guard so a partial-restore baseline missing any
    one table does not crash the ``ALTER TABLE``.  Single tail
    ``db.commit()`` after the guarded DDL — same shape as v7 / v8 / v9 /
    v10; the ``schema_version`` row is written by ``_apply_migrations``
    after this returns, and the guards make a crash-replay safe.
    """
    for table, index_name in _PRINCIPAL_TABLES:
        cursor = await db.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name=?",
            (table,),
        )
        if not await cursor.fetchone():
            continue
        cursor = await db.execute(f"PRAGMA table_info({table})")
        existing = {row[1] for row in await cursor.fetchall()}
        if "principal_id" not in existing:
            await db.execute(
                f"ALTER TABLE {table} ADD COLUMN "
                f"principal_id TEXT NOT NULL DEFAULT '{DEFAULT_PRINCIPAL_ID}'",
            )
        await db.execute(
            f"CREATE INDEX IF NOT EXISTS {index_name} "
            f"ON {table}(principal_id)",
        )

    await db.commit()
