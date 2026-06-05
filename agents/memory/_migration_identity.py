"""RFC 0031 amendment (F-7 Option D, ISSUE-0093) migration v13 — person
``identity`` column on ``relationships``.

Split out of :mod:`agents.memory._migration_handlers` to keep that module
under the project's 500-line review-friendly cap (see
``scripts/checks/file_size.py``), mirroring the v8–v12 splits.

**What & why.** F-7 closed a cross-room recall *seam* (Option A,
[#550](https://github.com/mkhomutov/Persatrix/pull/550)) by special-casing
``contact:*`` *note* recall to bypass the session filter — a topic-prefix
workaround threaded through *recall*, not a property of *where identity
lives*.  Option D moves person identity (name, role, stable preferences)
onto the genuinely cross-room **relationship** tier, whose primary key
deliberately omits ``session_id`` — so scope is a property of the tier and
the seam cannot recur by construction.  This migration is the storage
foundation: a single nullable ``identity TEXT`` (JSON) column.

**Why no table rebuild (unlike v11/v12).**  ``identity`` is *not* a key
column — it is per-row payload, like ``notes`` / ``trust_score``.  v11
(``principal_id``) and v12 (``epoch_id``) rebuilt ``relationships`` because
those axes had to enter the *primary key* to give each tenant / epoch a
physically distinct aggregate row; identity needs no such isolation (there
is exactly one identity per ``(participant tuple, principal, epoch)`` row
already).  So this follows the simple v7 ``ADD COLUMN`` skeleton, not the
12-step rebuild.

Idempotency / partial-restore safety: identical skeleton to v7 — the
``ALTER TABLE ... ADD COLUMN`` statement predates ``IF NOT EXISTS`` in
SQLite < 3.35, so a ``sqlite_master`` existence check short-circuits a
partial-restore baseline missing the table, and a ``PRAGMA table_info``
column check makes a crash-replay a clean no-op.  No index is created:
identity is never a *filter* column — it is read alongside the relationship
row that recall already anchors on the participant tuple, so an index would
be dead weight.
"""

from __future__ import annotations

import aiosqlite


async def _apply_migration_13(db: aiosqlite.Connection) -> None:
    """RFC 0031 amendment (F-7 Option D): ``identity`` column on relationships.

    Additive, nullable, no primary-key rebuild (see module docstring).
    Guarded by the same ``sqlite_master`` + ``PRAGMA table_info`` pattern as
    v7 so a partial-restore baseline missing the ``relationships`` table, or
    a crash-replay where the column already exists, does not crash the
    ``ADD COLUMN``.  Single tail ``db.commit()``; the ``schema_version`` row
    is written by ``_apply_migrations`` after this returns.
    """
    cursor = await db.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='relationships'",
    )
    if await cursor.fetchone():
        cursor = await db.execute("PRAGMA table_info(relationships)")
        existing = {row[1] for row in await cursor.fetchall()}
        if "identity" not in existing:
            await db.execute(
                "ALTER TABLE relationships ADD COLUMN identity TEXT",
            )

    await db.commit()
