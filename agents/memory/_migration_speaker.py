"""ISSUE-0131 migration v18 — the ``speaker_id`` column on the two
close-derived tiers (``episodes`` / ``facts``).

Split out of :mod:`agents.memory._migration_handlers` to keep that module
under the project's 500-line review-friendly cap (see
``scripts/checks/file_size.py``), mirroring the v8–v17 splits.

**What & why.**  Derived memory records WHAT was said and WHERE, never
WHO said it — the ISSUE-0131 defect.  The v0.3.15 residuals PR 3 keys
the :class:`~agents.memory.interaction_tracker.InteractionTracker`
``(principal, speaker, scope)``, so every close-derived record is
single-speaker by construction; this migration adds the column that
key half is PROJECTED onto at close.  Only the two tiers a group close
writes gain it (``store_episode`` → ``store_extracted_facts``) — the
``interactions`` TABLE is the relationship-tier log, written only by
``record_closed_interaction``, which returns early for every non-DM
scope, and the in-memory :class:`Interaction` dataclass needs no
migration at all (the two nearby wrong targets the residuals plan
names explicitly).

**Dormant-rail note (the v0.3.14 PR 1 / PR 2 split).**  This migration
lands with NO writer: residuals PR 4 — the close-path binding — is the
consumer that stamps ``interaction.speaker_id`` onto the rows, so the
column ships ahead of the code reading it (the v0.3.15 plan's
"no migration lands after its consumer" acceptance line).  Until then
every row's ``speaker_id`` is NULL.

**Why nullable, no backfill.**  A pre-v18 row's speaker is genuinely
unknowable — the aggregate it was derived from spanned every speaker in
the room, which is the defect itself — and attributing it after the
fact would need exactly the model-elected attribution the Phase 0b
scope lock forbids.  ``NULL`` = "derived before the speaker axis
existed / no speaker" is the honest value, the same posture as v16's
nullable ``source_channel_id``.  (Contrast v11's ``principal_id
DEFAULT 'local'``: there the default IS the correct single-tenant
answer; no such answer exists for a speaker.)

**Why no index.**  The speaker is an attribution surface, not a recall
filter: it is read alongside rows recall already anchors on
scope/session/principal, so an index would be dead weight (the v13
``identity`` precedent).  If a per-speaker recall predicate lands
later, its PR adds the index it needs.

Idempotency / partial-restore safety: identical skeleton to v7/v13 —
``ALTER TABLE ... ADD COLUMN`` predates ``IF NOT EXISTS`` in
SQLite < 3.35, so a ``sqlite_master`` existence check short-circuits a
partial-restore baseline missing a table, and a ``PRAGMA table_info``
column check makes a crash-replay a clean no-op.
"""

from __future__ import annotations

import aiosqlite

# The two close-derived tiers (module docstring) — deliberately NOT
# ``notes`` (reflection-written, not close-derived) and NOT the
# ``interactions`` table (DM-only relationship log).
_SPEAKER_TABLES: tuple[str, ...] = ("episodes", "facts")


async def _apply_migration_18(db: aiosqlite.Connection) -> None:
    """ISSUE-0131: nullable ``speaker_id`` on ``episodes`` + ``facts``.

    Additive, nullable, no backfill, no index, no primary-key rebuild
    (see module docstring).  Guarded per table by the v7/v13
    ``sqlite_master`` + ``PRAGMA table_info`` pattern.  Single tail
    ``db.commit()``; the ``schema_version`` row is written by
    ``_apply_migrations`` after this returns.
    """
    for table in _SPEAKER_TABLES:
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        )
        if await cursor.fetchone() is None:
            continue
        cursor = await db.execute(f"PRAGMA table_info({table})")
        existing = {row[1] for row in await cursor.fetchall()}
        if "speaker_id" not in existing:
            await db.execute(
                f"ALTER TABLE {table} ADD COLUMN speaker_id TEXT",
            )

    await db.commit()
