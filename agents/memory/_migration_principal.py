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

Four of the five tables key on a UUID primary key, so the tenant axis is
just an extra column + residual filter there.  ``relationships`` is the
exception: it keys on the participant tuple ``(participant_id,
participant_type, other_participant_id, other_participant_type)``, so a
bare ``ADD COLUMN`` would leave a *single* aggregate row per tuple that
the first tenant to touch it owns — a second tenant's ``ON CONFLICT DO
UPDATE`` would then mutate the first tenant's ``trust_score`` and the
strict-equality recall filter would silently mask the bleed (ISSUE-0081
PR 3 review H2).  So ``relationships`` is **rebuilt** with ``principal_id``
*in the primary key*, making each ``(participant tuple, principal)`` a
distinct row.  ``session_id`` stays out of the key by design: the
aggregate row is cross-session shared (first-seen tag), and per-session
views derive from the ``interactions`` table — only the tenant axis needs
physical row isolation.

The ``DEFAULT 'local'`` value is :data:`agents.principal_id.DEFAULT_PRINCIPAL_ID`
— the single-tenant principal every unauthenticated deployment uses.
Pre-existing rows upgrade to it with no backfill UPDATE.  Unlike the
``'legacy'`` session default this is **not** a cross-tenant carve-out:
the recall predicate (:func:`agents.memory._principal_filter.principal_eq_clause`)
is strict equality, so once a second tenant exists, ``'local'`` rows are
visible only to the ``'local'`` principal.

Idempotency / partial-restore safety: identical skeleton to v7 / v9 /
v10 — ``ALTER TABLE ... ADD COLUMN`` predates ``IF NOT EXISTS`` in
SQLite < 3.35, so each UUID-keyed table is guarded by a ``sqlite_master``
existence check (short-circuits a partial-restore baseline whose
``schema_version`` is recorded past v11 but lacks a given table) and a
``PRAGMA table_info`` column check before the ``ADD COLUMN``.  The
``relationships`` rebuild is guarded by inspecting whether ``principal_id``
is already part of the primary key, so a crash-replay is a clean no-op.
The per-table ``CREATE INDEX IF NOT EXISTS`` is the standalone
session-index analogue: it keeps the tiers uniform and gives a future
principal-scoped maintenance op an index without a follow-up migration;
it is not the index recall seeks (the recall queries are anchored on
``agent_id`` / the participant tuple and apply ``principal_id`` as a
residual filter).
"""

from __future__ import annotations

import aiosqlite

from ..principal_id import DEFAULT_PRINCIPAL_ID

#: ``(table, principal-index name)`` pairs for the four UUID-keyed tiers.
#: ``relationships`` is **not** here — it keys on the participant tuple and
#: is rebuilt with ``principal_id`` in the primary key (see module
#: docstring + :func:`_rebuild_relationships_principal_pk`).  Both members
#: of each pair are trusted internal literals — never user input — so the
#: f-string interpolation below is safe (same contract as the v7 handler).
_PRINCIPAL_TABLES: tuple[tuple[str, str], ...] = (
    ("episodes", "idx_episodes_principal"),
    ("facts", "idx_facts_principal"),
    ("notes", "idx_notes_principal"),
    ("interactions", "idx_interactions_principal"),
)


async def _rebuild_relationships_principal_pk(
    db: aiosqlite.Connection,
) -> None:
    """Rebuild ``relationships`` with ``principal_id`` in the primary key.

    A bare ``ADD COLUMN`` cannot give the tenant axis physical row
    isolation, because the participant-tuple primary key still admits only
    one row per ``(participant_id, participant_type, other_participant_id,
    other_participant_type)``.  This rebuilds the table so the key is the
    participant tuple **plus** ``principal_id`` — each tenant gets its own
    aggregate row, and a cross-tenant ``ON CONFLICT DO UPDATE`` can no
    longer mutate a foreign tenant's ``trust_score`` (ISSUE-0081 PR 3
    review H2).  Mirrors the v4 12-step rebuild, including the
    ``relationships_new`` crash-recovery staging guard.

    Idempotent: if ``principal_id`` is already part of the primary key the
    rebuild is skipped (clean crash-replay).  ``session_id`` is preserved
    as a plain column (deliberately *not* in the key — the aggregate row
    is cross-session shared; only the tenant axis needs row isolation).
    """
    # Crash recovery: a prior run that died after CREATE but before RENAME
    # leaves the staging table behind.  Recover the way v4 does.
    cursor = await db.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='relationships_new'",
    )
    if await cursor.fetchone():
        cursor = await db.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='relationships'",
        )
        if await cursor.fetchone():
            # Both exist — staging copy was incomplete; discard it.
            await db.execute("DROP TABLE relationships_new")
        else:
            await db.execute(
                "ALTER TABLE relationships_new RENAME TO relationships",
            )

    cursor = await db.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='relationships'",
    )
    if not await cursor.fetchone():
        # Partial-restore baseline missing the table — nothing to rebuild.
        return

    cursor = await db.execute("PRAGMA table_info(relationships)")
    info = await cursor.fetchall()
    pk_cols = {row[1] for row in info if row[5] > 0}
    if "principal_id" in pk_cols:
        # Already rebuilt (idempotent replay).
        return
    # A baseline could already carry principal_id as a plain column (e.g. a
    # pre-amendment v11 run); carry its values forward, else default 'local'.
    has_principal_col = any(row[1] == "principal_id" for row in info)
    principal_src = (
        "principal_id" if has_principal_col else f"'{DEFAULT_PRINCIPAL_ID}'"
    )

    await db.execute(
        f"""
        CREATE TABLE relationships_new (
            participant_id TEXT NOT NULL,
            participant_type TEXT NOT NULL DEFAULT 'agent',
            other_participant_id TEXT NOT NULL,
            other_participant_type TEXT NOT NULL DEFAULT 'agent',
            trust_score REAL DEFAULT 0.5,
            interaction_count INTEGER DEFAULT 0,
            last_interaction_at REAL,
            notes TEXT,
            session_id TEXT NOT NULL DEFAULT 'legacy',
            principal_id TEXT NOT NULL DEFAULT '{DEFAULT_PRINCIPAL_ID}',
            PRIMARY KEY (participant_id, participant_type,
                         other_participant_id, other_participant_type,
                         principal_id)
        )
        """,  # noqa: S608 — DEFAULT_PRINCIPAL_ID is a trusted constant.
    )
    await db.execute(
        f"""
        INSERT INTO relationships_new
            (participant_id, participant_type,
             other_participant_id, other_participant_type,
             trust_score, interaction_count, last_interaction_at, notes,
             session_id, principal_id)
        SELECT participant_id, participant_type,
               other_participant_id, other_participant_type,
               trust_score, interaction_count, last_interaction_at, notes,
               session_id, {principal_src}
        FROM relationships
        """,  # noqa: S608 — principal_src is a column name or trusted literal.
    )
    await db.execute("DROP TABLE relationships")
    await db.execute(
        "ALTER TABLE relationships_new RENAME TO relationships",
    )
    # Recreate the indexes the rebuilt table lost with the DROP (same set
    # the v4 + session/principal migrations maintain).
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_relationships_participant "
        "ON relationships(participant_id, participant_type)",
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_rel_session "
        "ON relationships(session_id)",
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_rel_principal "
        "ON relationships(principal_id)",
    )


async def _apply_migration_11(db: aiosqlite.Connection) -> None:
    """ISSUE-0081 PR 3: ``principal_id`` on all five persona-memory tiers.

    ``relationships`` is rebuilt with ``principal_id`` in its primary key
    (:func:`_rebuild_relationships_principal_pk`); the other four tiers —
    all UUID-keyed — gain ``principal_id TEXT NOT NULL DEFAULT
    '<DEFAULT_PRINCIPAL_ID>'`` and an ``idx_<tier>_principal`` index.

    Each UUID-keyed table is handled independently with the v7
    ``sqlite_master`` + ``PRAGMA table_info`` guard so a partial-restore
    baseline missing any one table does not crash the ``ALTER TABLE``.
    Single tail ``db.commit()`` after the guarded DDL — same shape as
    v7 / v8 / v9 / v10; the ``schema_version`` row is written by
    ``_apply_migrations`` after this returns, and the guards make a
    crash-replay safe.
    """
    await _rebuild_relationships_principal_pk(db)
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
