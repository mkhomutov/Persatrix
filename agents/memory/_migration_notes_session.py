"""RFC 0031 Phase 2 PR 1 migration v9 — ``session_id`` on notes.

Split out of :mod:`agents.memory._migration_handlers` so the parent
module stays under the project's 500-line review-friendly cap (see
``scripts/checks/file_size.py``).  The split mirrors the
:mod:`agents.memory._migration_facts` separation — one RFC-scoped
helper, re-exported by the parent module for backwards compatibility.

Phase 1's migration v7 tagged ``episodes`` and ``relationships``, and
RFC 0026's v8 tagged ``facts``; the ``notes`` tier (migration v2) was the
last persona-memory recall surface with no session dimension.  Without
this column the Phase 2 recall filter would have no non-degenerate
``notes.session_id`` to filter on, re-introducing F-3 on the notes prompt
surface even after the other three tiers are scoped.
"""

from __future__ import annotations

import aiosqlite


async def _apply_migration_9(db: aiosqlite.Connection) -> None:
    """RFC 0031 Phase 2 PR 1: ``session_id`` on the ``notes`` tier.

    Adds ``session_id TEXT NOT NULL DEFAULT 'legacy'`` to ``notes`` and
    creates ``idx_notes_session`` so Phase 2 per-session recall has a
    column + index pair to filter on without a follow-up migration —
    identical contract to v7's ``episodes`` / ``relationships`` halves
    and v8's ``facts`` table.

    The ``'legacy'`` default is the synthetic carve-out shared across the
    tiers: it matches ``channels.DefaultSessionID`` so pre-RFC note rows
    upgrade cleanly without a backfill UPDATE and stay visible from every
    session under the Phase 2 ``legacy`` carve-out.

    Idempotency: ``ALTER TABLE ... ADD COLUMN`` predates ``IF NOT EXISTS``
    in SQLite < 3.35 (Persatrix does not require that minimum), so the
    column existence is checked via ``PRAGMA table_info`` first — same
    discipline as v5/v6/v7.  The ``sqlite_master`` guard short-circuits a
    partial-restore baseline (``schema_version`` recorded up to v8 but no
    ``notes`` table) so the ``ALTER TABLE`` cannot crash.

    The ``notes`` FTS5 mirror + sync triggers (migration v2) index
    ``topic`` / ``content`` / ``tags_json`` only, not ``session_id``, so
    the plain ``ADD COLUMN`` + B-tree index here does not touch the
    virtual table or its triggers.
    """
    cursor = await db.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='notes'",
    )
    if await cursor.fetchone():
        cursor = await db.execute("PRAGMA table_info(notes)")
        existing = {row[1] for row in await cursor.fetchall()}
        if "session_id" not in existing:
            await db.execute(
                "ALTER TABLE notes ADD COLUMN "
                "session_id TEXT NOT NULL DEFAULT 'legacy'",
            )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_notes_session "
            "ON notes(session_id)",
        )

    await db.commit()
