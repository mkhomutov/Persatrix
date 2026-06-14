"""ISSUE-0102 PR 2 migration v15 — the queryable
``governance_interaction_id`` column on ``episodes``.

Split out of :mod:`agents.memory._migration_handlers` to keep that module
under the project's 500-line review-friendly cap (see
``scripts/checks/file_size.py``), mirroring the v8–v14 splits.

**What & why.**  PR 1 persisted the RFC 0030 *governance* interaction id
(the channel-side id the end-vote close logs carry) into the episode
*context blob* and surfaced it for display.  But the closed-interaction read
filter matches the ``interaction_id`` *column* — the persona's agent-side
RFC 0020 episode id, a different namespace — so the natural diagnostic move
(``agent interactions --interaction-id <governance-id>``) still returned
nothing.  This migration promotes the governance id to a real column so the
read filter can match it (``interaction_id = ? OR governance_interaction_id =
?``), making the channel-side id directly look-up-able.

This is a **schema add + one-time data backfill** in one version: the column
is added (additive, nullable — no primary-key rebuild, like v5/v13), then
existing rows are backfilled from their own ``context_json`` so a row written
by PR 1 (governance id in the blob only) becomes look-up-able without
waiting for a fresh write.  A row whose context carries no governance id
(DM / thread / non-channel / pre-PR-1) keeps ``NULL``.

**Why backfill in Python (not ``json_extract``).**  The codebase makes no
other use of SQLite's JSON1 ``json_extract``; the v14 backfill set the
precedent of reading rows and transforming in Python with individual
``db.execute`` calls + a single tail commit (the ``executescript``-implicit-
COMMIT caveat in :mod:`agents.memory.migrations` does not apply to a callable
handler).  A ``LIKE`` prefilter skips rows that cannot carry the key, so the
backfill only parses the rows that might.

Idempotency / partial-restore safety: identical skeleton to v5/v13 — a
``sqlite_master`` existence check short-circuits a partial-restore baseline
missing the ``episodes`` table, and a ``PRAGMA table_info`` column check
makes a crash-replay a clean no-op (the column is added at most once; the
backfill only touches rows whose column is still ``NULL``, so re-running
merges nothing new).  No index is created: like the agent-side
``interaction_id`` the filter already matches, the per-agent closed-row set
is small and bounded by the recall limit, so an index would be dead weight.
"""

from __future__ import annotations

import json

import aiosqlite

__all__ = ["_apply_migration_15"]

#: The context-blob key PR 1's close path stamps the governance id under.
_CONTEXT_KEY = "governance_interaction_id"


async def _apply_migration_15(db: aiosqlite.Connection) -> None:
    """ISSUE-0102 PR 2: add + backfill ``episodes.governance_interaction_id``.

    No-op (clean return) when the ``episodes`` table is absent (partial-
    restore baseline).  Single tail ``db.commit()``; ``_apply_migrations``
    records ``schema_version`` after this returns.
    """
    cursor = await db.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='episodes'",
    )
    if not await cursor.fetchone():
        return

    cursor = await db.execute("PRAGMA table_info(episodes)")
    existing = {row[1] for row in await cursor.fetchall()}
    if _CONTEXT_KEY not in existing:
        await db.execute(
            f"ALTER TABLE episodes ADD COLUMN {_CONTEXT_KEY} TEXT",
        )

    await _backfill_from_context(db)
    await db.commit()


async def _backfill_from_context(db: aiosqlite.Connection) -> None:
    """Populate the new column from each row's ``context_json`` (PR 1 shape).

    Only rows whose column is still ``NULL`` and whose blob could mention the
    key are read; a non-empty parsed value is written through.  Re-running
    finds nothing left to backfill (the rows it wrote are no longer ``NULL``),
    so the transform is idempotent.
    """
    cursor = await db.execute(
        f"""
        SELECT id, context_json
        FROM episodes
        WHERE {_CONTEXT_KEY} IS NULL
          AND context_json LIKE '%{_CONTEXT_KEY}%'
        """,
    )
    rows = await cursor.fetchall()
    for episode_id, context_json in rows:
        if not context_json:
            continue
        try:
            ctx = json.loads(context_json)
        except (ValueError, TypeError):
            continue
        if not isinstance(ctx, dict):
            continue
        value = ctx.get(_CONTEXT_KEY)
        if not isinstance(value, str) or not value:
            continue
        await db.execute(
            f"UPDATE episodes SET {_CONTEXT_KEY} = ? WHERE id = ?",
            (value, episode_id),
        )
