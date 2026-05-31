"""Single-row episode CRUD helpers for :class:`agents.memory.episodic.EpisodicMemory`.

Split out of :mod:`agents.memory.episodic` so the parent module stays
under the project's 500-line review-friendly cap (see
``scripts/checks/file_size.py``), mirroring the
:mod:`agents.memory.episodic_queries` /
:mod:`agents.memory.episodic_retention` precedent — the by-id fetch,
count, and delete sit beside the recall/insert SQL rather than in the
dispatching class body.

All three are agent-scoped (RFC 0008 §H ACL): they filter on
``agent_id`` so cross-agent access is impossible.  They are deliberately
*not* session / principal / epoch scoped — they are by-id administrative
primitives, not the per-request recall path (the same maintenance-surface
carve-out the eviction / retention sweeps inherit).
"""

from __future__ import annotations

import aiosqlite

from .episodic_queries import EPISODE_SELECT, Episode, row_to_episode

__all__ = ["count_episodes", "delete_episode", "get_episode"]


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
