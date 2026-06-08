"""Closed-interaction summary reads (v0.3.8 interaction-summary surface).

The read side of the surface: an interaction that has been closed and
persisted (any trigger — idle, structural/end-vote, or the RFC 0030
Layer 1 cost ceiling) carries a non-NULL ``closed_at`` and an
``interaction_id`` on its :class:`agents.memory.episodic_queries.Episode`
row. This module reads those rows back, newest-first, so the web console
and CLI can render the synthesised RFC 0020 per-interaction summary.

Split out of :mod:`agents.memory.episodic_queries` /
:class:`agents.memory.episodic.EpisodicMemory` (both at the 500-line
review cap) as a free-function module — the same extraction idiom as
:mod:`agents.persona_runtime.close_path`. Surface-only: nothing here
(re)generates a summary, and the ``"[interaction summary unavailable]"``
failure sentinel is returned verbatim (SS3) rather than filtered, so a
failed summary is shown honestly instead of hidden.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import aiosqlite

from .episodic_queries import EPISODE_SELECT, MAX_RECALL_LIMIT, row_to_episode

if TYPE_CHECKING:
    from .episodic import EpisodicMemory
    from .episodic_queries import Episode

__all__ = ["recall_closed_interactions", "closed_interactions"]


async def recall_closed_interactions(
    db: aiosqlite.Connection,
    agent_id: str,
    *,
    limit: int,
    scope: str | None = None,
    interaction_id: str | None = None,
) -> list[aiosqlite.Row]:
    """Return closed-interaction episode rows newest-first (by ``closed_at``).

    Filters: ``scope`` restricts to one RFC 0020 scope (``None`` spans all
    scopes for the agent); ``interaction_id`` fetches exactly one
    interaction (``None`` lists). ``limit`` is clamped to
    :data:`MAX_RECALL_LIMIT`. The failure sentinel is included (SS3).
    """
    clamped = max(1, min(limit, MAX_RECALL_LIMIT))
    filters = ""
    params: list[object] = [agent_id]
    if scope is not None:
        filters += " AND scope = ?"
        params.append(scope)
    if interaction_id is not None:
        filters += " AND interaction_id = ?"
        params.append(interaction_id)
    params.append(clamped)
    async with db.execute(
        f"""
        SELECT {EPISODE_SELECT}
        FROM episodes
        WHERE agent_id = ?
          AND closed_at IS NOT NULL
          AND interaction_id IS NOT NULL
          {filters}
        ORDER BY closed_at DESC
        LIMIT ?
        """,
        tuple(params),
    ) as cursor:
        return list(await cursor.fetchall())


async def closed_interactions(
    episodic: EpisodicMemory,
    *,
    limit: int = 20,
    scope: str | None = None,
    interaction_id: str | None = None,
) -> list[Episode]:
    """Read an agent's closed-interaction summaries as :class:`Episode` rows.

    Thin facade over :func:`recall_closed_interactions` that resolves the
    agent's open SQLite connection and maps rows to the
    :class:`Episode` dataclass (exposing ``summary``, ``closed_at``,
    ``turn_count``, ``scope`` and the ``close_reason`` carried in
    ``context``).
    """
    rows = await recall_closed_interactions(
        episodic._ensure_db(), episodic._agent_id,
        limit=limit, scope=scope, interaction_id=interaction_id,
    )
    return [row_to_episode(row) for row in rows]
