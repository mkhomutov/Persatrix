"""By-id episode CRUD helpers for :class:`agents.memory.episodic.EpisodicMemory`.

Split out of :mod:`agents.memory.episodic` so the parent module stays
under the project's 500-line review-friendly cap (see
``scripts/checks/file_size.py``), mirroring the
:mod:`agents.memory.episodic_queries` /
:mod:`agents.memory.episodic_retention` precedent — the by-id fetch,
count, delete and access bump sit beside the recall/insert SQL rather
than in the dispatching class body.

All of them are agent-scoped (RFC 0008 §H ACL): they filter on
``agent_id`` so cross-agent access is impossible.  They are deliberately
*not* session / principal / epoch scoped — they are by-id primitives, not
the per-request recall path (the same maintenance-surface carve-out the
eviction / retention sweeps inherit).  The access bump belongs here
because it acts on ids a recall has already chosen (ISSUE-0163).
"""

from __future__ import annotations

import time
from collections.abc import Sequence

import aiosqlite

from .episodic_queries import EPISODE_SELECT, Episode, row_to_episode

__all__ = ["count_episodes", "delete_episode", "get_episode", "reinforce_episodes"]


async def get_episode(
    db: aiosqlite.Connection, agent_id: str, episode_id: str,
) -> Episode | None:
    """Retrieve a single episode by ID (agent-scoped)."""
    async with db.execute(
        f"SELECT {EPISODE_SELECT} FROM episodes WHERE id = ? AND agent_id = ?",
        (episode_id, agent_id),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    return row_to_episode(row)


async def count_episodes(db: aiosqlite.Connection, agent_id: str) -> int:
    """Return the number of episodes for this agent."""
    async with db.execute(
        "SELECT COUNT(*) FROM episodes WHERE agent_id = ?",
        (agent_id,),
    ) as cursor:
        row = await cursor.fetchone()
    return row[0] if row else 0


async def delete_episode(
    db: aiosqlite.Connection, agent_id: str, episode_id: str,
) -> bool:
    """Delete a single episode by ID, agent-scoped. RFC 0008 PR 3a / N5."""
    cursor = await db.execute(
        "DELETE FROM episodes WHERE id = ? AND agent_id = ?",
        (episode_id, agent_id),
    )
    await db.commit()
    return (cursor.rowcount or 0) > 0


async def reinforce_episodes(
    db: aiosqlite.Connection, agent_id: str, episode_ids: Sequence[str],
) -> float:
    """Count one more use of each episode in *episode_ids*.

    The access bump :meth:`EpisodicMemory.recall` applies to what it
    returns: ``access_count + 1`` and ``last_accessed_at`` set to now.
    ``access_count`` multiplies the recall ranking score, so the caller
    passes only episodes that were used.  Returns the timestamp written,
    for a caller that refreshes its in-memory rows; an empty list writes
    nothing.  The id list is a turn's recall, at most
    ``MAX_RECALL_LIMIT`` rows, so it needs none of the chunking the facts
    tier's reinforcement does.
    """
    now = time.time()
    if not episode_ids:
        return now
    placeholders = ",".join("?" for _ in episode_ids)
    await db.execute(
        f"UPDATE episodes SET access_count = access_count + 1, "
        f"last_accessed_at = ? WHERE agent_id = ? AND id IN ({placeholders})",
        [now, agent_id, *episode_ids],
    )
    await db.commit()
    return now
