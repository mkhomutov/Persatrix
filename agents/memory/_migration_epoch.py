"""ISSUE-0085 PR 2 migration v12 — ``epoch_id`` on every memory tier.

Split out of :mod:`agents.memory._migration_handlers` to keep that module
under the project's 500-line review-friendly cap (see
``scripts/checks/file_size.py``), mirroring the v8 / v9 / v10 / v11 splits.

Where ``principal_id`` (migration v11) answers "which tenant owns this
row?", ``epoch_id`` answers "which test run / logical branch wrote it?"
(ISSUE-0085 — the structural half of the F-3 fix the scope-axes reframing
moves off the session axis).  This migration adds the column to **all
five** persona-memory tables in one version so the run-isolation dimension
lands atomically across tiers — ``episodes`` / ``relationships`` /
``facts`` / ``notes`` / ``interactions``.

Four of the five tables key on a UUID primary key, so the epoch axis is
just an extra column + residual filter there.  ``relationships`` is the
exception: after v11 it keys on the participant tuple **plus**
``principal_id``, so a bare ``ADD COLUMN`` would leave a *single* aggregate
row per ``(participant tuple, principal)`` that the first epoch to touch it
owns — a rerun under a fresh epoch's ``ON CONFLICT DO UPDATE`` would then
mutate the prior run's ``trust_score`` and the strict-equality recall
filter would silently mask the bleed (the same hazard v11 closed for
tenants).  So ``relationships`` is **rebuilt** with ``epoch_id`` *in the
primary key* alongside ``principal_id``, making each ``(participant tuple,
principal, epoch)`` a distinct row.  ``session_id`` stays out of the key by
design (it is the continuity carve-out, cross-run shared): only the
isolation axes — tenant and epoch — need physical row separation.

The ``DEFAULT 'live'`` value is :data:`agents.epoch_id.DEFAULT_EPOCH_ID`
— the epoch every production / untagged deployment uses.  Pre-existing rows
upgrade to it with no backfill UPDATE.  Unlike the ``'legacy'`` session
default this is **not** a cross-epoch carve-out: the recall predicate
(:func:`agents.memory._epoch_filter.epoch_eq_clause`, PR 3) is strict
equality with no ``"*"`` bypass, so once a second epoch exists, ``'live'``
rows are visible only to the ``'live'`` epoch.

Idempotency / partial-restore safety: identical skeleton to v7 / v9 /
v10 / v11 — ``ALTER TABLE ... ADD COLUMN`` predates ``IF NOT EXISTS`` in
SQLite < 3.35, so each UUID-keyed table is guarded by a ``sqlite_master``
existence check (short-circuits a partial-restore baseline whose
``schema_version`` is recorded past v12 but lacks a given table) and a
``PRAGMA table_info`` column check before the ``ADD COLUMN``.  The
``relationships`` rebuild is guarded by inspecting whether ``epoch_id`` is
already part of the primary key, so a crash-replay is a clean no-op.  The
per-table ``CREATE INDEX IF NOT EXISTS`` keeps the tiers uniform and gives
a future epoch-scoped maintenance op an index without a follow-up
migration; it is not the index recall seeks (recall anchors on
``agent_id`` / the participant tuple and applies ``epoch_id`` as a residual
filter, the same shape as ``principal_id``).
"""

from __future__ import annotations

import aiosqlite

from ..epoch_id import DEFAULT_EPOCH_ID

#: ``(table, epoch-index name)`` pairs for the four UUID-keyed tiers.
#: ``relationships`` is **not** here — after v11 it keys on the participant
#: tuple plus ``principal_id`` and is rebuilt with ``epoch_id`` added to the
#: primary key (see module docstring + :func:`_rebuild_relationships_epoch_pk`).
#: Both members of each pair are trusted internal literals — never user input
#: — so the f-string interpolation below is safe (same contract as v7/v11).
_EPOCH_TABLES: tuple[tuple[str, str], ...] = (
    ("episodes", "idx_episodes_epoch"),
    ("facts", "idx_facts_epoch"),
    ("notes", "idx_notes_epoch"),
    ("interactions", "idx_interactions_epoch"),
)


async def _rebuild_relationships_epoch_pk(
    db: aiosqlite.Connection,
) -> None:
    """Rebuild ``relationships`` with ``epoch_id`` in the primary key.

    After v11 the key is the participant tuple **plus** ``principal_id``; a
    bare ``ADD COLUMN`` cannot give the epoch axis physical row isolation
    because that key still admits only one row per ``(participant tuple,
    principal)``.  This rebuilds the table so the key is the participant
    tuple **plus** ``principal_id`` **plus** ``epoch_id`` — each epoch gets
    its own aggregate row, and a rerun's ``ON CONFLICT DO UPDATE`` can no
    longer mutate the prior run's ``trust_score`` (the v11 review-H2 hazard,
    re-applied to the epoch axis).  Mirrors the v11 rebuild, including the
    ``relationships_new`` crash-recovery staging guard.

    Idempotent: if ``epoch_id`` is already part of the primary key the
    rebuild is skipped (clean crash-replay).  ``session_id`` is preserved as
    a plain column (deliberately *not* in the key — the continuity carve-out
    is cross-run shared; only the isolation axes need row separation).
    """
    # Crash recovery: a prior run that died after CREATE but before RENAME
    # leaves the staging table behind.  Recover the way v4 / v11 do.
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
    if "epoch_id" in pk_cols:
        # Already rebuilt (idempotent replay).
        return
    cols = {row[1] for row in info}
    # ``principal_id`` is guaranteed present + in-key by v11, but guard
    # defensively against an odd baseline: carry it forward when present,
    # else default 'local' (mirrors v11's own has-column carry-forward).
    has_principal_col = "principal_id" in cols
    principal_src = "principal_id" if has_principal_col else "'local'"
    # A baseline could already carry epoch_id as a plain column (e.g. a
    # pre-amendment v12 run); carry its values forward, else default 'live'.
    has_epoch_col = "epoch_id" in cols
    epoch_src = "epoch_id" if has_epoch_col else f"'{DEFAULT_EPOCH_ID}'"

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
            principal_id TEXT NOT NULL DEFAULT 'local',
            epoch_id TEXT NOT NULL DEFAULT '{DEFAULT_EPOCH_ID}',
            PRIMARY KEY (participant_id, participant_type,
                         other_participant_id, other_participant_type,
                         principal_id, epoch_id)
        )
        """,  # noqa: S608 — DEFAULT_EPOCH_ID is a trusted constant.
    )
    await db.execute(
        f"""
        INSERT INTO relationships_new
            (participant_id, participant_type,
             other_participant_id, other_participant_type,
             trust_score, interaction_count, last_interaction_at, notes,
             session_id, principal_id, epoch_id)
        SELECT participant_id, participant_type,
               other_participant_id, other_participant_type,
               trust_score, interaction_count, last_interaction_at, notes,
               session_id, {principal_src}, {epoch_src}
        FROM relationships
        """,  # noqa: S608 — *_src are column names or trusted literals.
    )
    await db.execute("DROP TABLE relationships")
    await db.execute(
        "ALTER TABLE relationships_new RENAME TO relationships",
    )
    # Recreate the indexes the rebuilt table lost with the DROP (same set
    # the v4 + session/principal migrations maintain, plus the epoch index).
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
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_rel_epoch "
        "ON relationships(epoch_id)",
    )


async def _apply_migration_12(db: aiosqlite.Connection) -> None:
    """ISSUE-0085 PR 2: ``epoch_id`` on all five persona-memory tiers.

    ``relationships`` is rebuilt with ``epoch_id`` in its primary key
    (:func:`_rebuild_relationships_epoch_pk`); the other four tiers — all
    UUID-keyed — gain ``epoch_id TEXT NOT NULL DEFAULT '<DEFAULT_EPOCH_ID>'``
    and an ``idx_<tier>_epoch`` index.

    Each UUID-keyed table is handled independently with the v7
    ``sqlite_master`` + ``PRAGMA table_info`` guard so a partial-restore
    baseline missing any one table does not crash the ``ALTER TABLE``.
    Single tail ``db.commit()`` after the guarded DDL — same shape as
    v7 / v8 / v9 / v10 / v11; the ``schema_version`` row is written by
    ``_apply_migrations`` after this returns, and the guards make a
    crash-replay safe.
    """
    await _rebuild_relationships_epoch_pk(db)
    for table, index_name in _EPOCH_TABLES:
        cursor = await db.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name=?",
            (table,),
        )
        if not await cursor.fetchone():
            continue
        cursor = await db.execute(f"PRAGMA table_info({table})")
        existing = {row[1] for row in await cursor.fetchall()}
        if "epoch_id" not in existing:
            await db.execute(
                f"ALTER TABLE {table} ADD COLUMN "
                f"epoch_id TEXT NOT NULL DEFAULT '{DEFAULT_EPOCH_ID}'",
            )
        await db.execute(
            f"CREATE INDEX IF NOT EXISTS {index_name} "
            f"ON {table}(epoch_id)",
        )

    await db.commit()
