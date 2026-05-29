"""RFC 0031 Phase 2 PR 5 migration v10 — ``session_id`` on interactions.

Split out of :mod:`agents.memory._migration_handlers` to keep that
module under the project's 500-line review-friendly cap (see
``scripts/checks/file_size.py``).  Mirrors the
:mod:`agents.memory._migration_facts` (v8) /
:mod:`agents.memory._migration_notes_session` (v9) separation — one
RFC-section-scoped helper, re-exported by the parent module.

Phase 2 PR 1 closed the ``notes`` write-path gap with migration v9.
The ``interactions`` table — fed by
:func:`agents.memory.relationship_mutations.record_interaction` and
read by :func:`agents.memory.relationship_queries.get_relationship_summary`
— was the last remaining persona-memory recall surface with no
session dimension after PR 3 filtered the parent ``relationships`` row
(`ISSUE-0080
<../../docs/issues/ISSUE-0080-relationship-recent-interactions-cross-session-leak.md>`_).
Without this column, ``recent_interactions`` and ``MIN(created_at)``
leak cross-session history into the persona prompt whenever the
relationship row is visible.
"""

from __future__ import annotations

import aiosqlite


async def _apply_migration_10(db: aiosqlite.Connection) -> None:
    """RFC 0031 Phase 2 PR 5: ``session_id`` on the ``interactions`` tier.

    Adds ``session_id TEXT NOT NULL DEFAULT 'legacy'`` to ``interactions``
    and creates ``idx_interactions_session`` for parity with the
    per-tier session indexes from v7 / v9 (``idx_episodes_session`` /
    ``idx_rel_session`` / ``idx_notes_session``).  The standalone index
    is **not** what serves the §D recall filter: the SELECTs in
    :func:`agents.memory.relationship_queries.get_relationship_summary`
    and :func:`~agents.memory.relationship_queries.get_all_relationships`
    are anchored on the participant 4-tuple, so ``EXPLAIN QUERY PLAN``
    shows them seeking ``idx_interactions_participant_lookup`` and
    applying ``session_id`` as a residual filter — do not drop that
    composite on the assumption this index covers recall.  The
    session-only index exists to keep the four tiers uniform and to give
    a future session-scoped maintenance/lifecycle op an index without a
    follow-up migration (the shape the notes per-session capacity prune
    already plans against ``idx_notes_session``).  The ``'legacy'``
    default matches the v7/v8/v9 carve-out so pre-RFC interaction rows
    upgrade cleanly with no backfill UPDATE and stay visible from every
    session under the carve-out.

    Idempotency: ``ALTER TABLE ... ADD COLUMN`` predates ``IF NOT EXISTS``
    in SQLite < 3.35; the column existence is checked via
    ``PRAGMA table_info`` first.  The ``sqlite_master`` guard short-
    circuits the partial-restore baseline (``schema_version`` recorded
    up to v9 but no ``interactions`` table) so the ``ALTER TABLE``
    cannot crash.  Same handler skeleton as v7 / v8 / v9.
    """
    cursor = await db.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='interactions'",
    )
    if await cursor.fetchone():
        cursor = await db.execute("PRAGMA table_info(interactions)")
        existing = {row[1] for row in await cursor.fetchall()}
        if "session_id" not in existing:
            await db.execute(
                "ALTER TABLE interactions ADD COLUMN "
                "session_id TEXT NOT NULL DEFAULT 'legacy'",
            )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_interactions_session "
            "ON interactions(session_id)",
        )

    await db.commit()
