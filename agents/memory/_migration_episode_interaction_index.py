"""ISSUE-0130 (b) migration v19 — index ``episodes(agent_id, interaction_id)``.

Split out of :mod:`agents.memory._migration_handlers` to keep that module
under the project's 500-line review-friendly cap (see
``scripts/checks/file_size.py``), mirroring the v8–v18 splits.

**What & why.**  Two callers run exactly the predicate
``WHERE agent_id = ? AND interaction_id = ?`` on ``episodes``, and until
this migration both scanned the agent's whole partition through
``idx_episodes_agent``:

* :func:`agents.memory.episodic_queries.update_episode_summary` — the
  close path's Phase 2, on EVERY close, live and replayed.  Since the
  ISSUE-0123 re-key that is once per SPEAKER per room rather than once
  per room, so its frequency grew in the same release; and
* :func:`agents.memory._episodic_replay_dedup.episode_exists_for_interaction`
  — the ISSUE-0130 shape-(b) re-derivation guard, once per replayed span,
  on the boot path.

The scan is linear in the agent's episode count, which for a persona has
no ceiling: the eviction loop runs only inside
:class:`~agents.memory.store.MemoryStore`, which personas never construct
(they build tiers through :mod:`agents.memory.personal_tiers`).  Measured
on file-backed SQLite with realistic 3 KB ``context_json`` rows —
0.64 ms per lookup at 1 000 episodes, 4.2 ms at 5 000, 15.8 ms at
20 000, against 0.002 ms indexed, where the guard's lookup plans as a
COVERING index scan.  Build cost is 20 ms and 520 KiB at 20 000 rows.

**Why a handler and not inline SQL.**  ``CREATE INDEX IF NOT EXISTS`` is
idempotent, so re-running it is safe and the registry's ``executescript``
path would have served — except for the baseline where ``episodes`` does
not exist at all.  ``_apply_migrations`` records v5+ as applied even when
handlers short-circuit (a DB whose ``schema_version`` starts at 4 with no
tables; ``TestEmptyEpisodesGuard`` pins it), and ``CREATE INDEX`` on a
missing table raises rather than no-opping.  So this takes the same
``sqlite_master`` guard every sibling handler uses.

**Why this is not a migration landing after its consumer.**  The v0.3.15
plan requires a migration to ship ahead of the code reading it.  An index
adds no column and changes no read RESULT — only the query plan — so
there is no reader to be ahead of; both consumers above already exist.
"""

from __future__ import annotations

import aiosqlite

__all__ = ["_apply_migration_19"]


async def _apply_migration_19(db: aiosqlite.Connection) -> None:
    """Create ``idx_episodes_interaction`` on ``(agent_id, interaction_id)``.

    Column order matters: ``agent_id`` leads because every caller filters
    it, and it keeps the index usable for the agent-scoped lookups that
    do not name an interaction.
    """
    cursor = await db.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='episodes'",
    )
    if not await cursor.fetchone():
        # Partial baseline (schema_version pre-recorded without the
        # table).  No DDL ran, so there is nothing to commit; the version
        # record is written by ``_apply_migrations`` after this returns,
        # matching every sibling handler's tail-commit contract.
        return

    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_episodes_interaction "
        "ON episodes(agent_id, interaction_id)",
    )
    # ...and drop the one it supersedes.  ``idx_episodes_agent(agent_id)``
    # is a strict prefix of the composite, so every predicate it served is
    # still served — SQLite simply plans them through the wider index.
    # Keeping both is not free: the dead B-tree is maintained on every
    # INSERT, which is once per close per speaker per room since the
    # ISSUE-0123 re-key.  Measured at 20 000 episodes with 3 KB
    # ``context_json`` rows: INSERT 0.0477 ms -> 0.0364 ms (**-24%**) once
    # dropped, against a 2.4% regression on the agent-only ``COUNT(*)``
    # (0.1580 -> 0.1618 ms), which stays a COVERING scan on the composite.
    # Closes on every turn; counts rarely.
    await db.execute("DROP INDEX IF EXISTS idx_episodes_agent")
    await db.commit()
