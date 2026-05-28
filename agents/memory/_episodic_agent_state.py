"""Agent-state DML helpers extracted from :mod:`episodic_queries`.

The ``agent_state`` table tracks the per-agent interaction counter and
the opaque ``persona_state_json`` blob.  Neither is recall-related;
the helpers used to live in ``episodic_queries.py`` only because that
file owned the shared :func:`aiosqlite` connection and predated the
RFC 0029 facade.  RFC 0031 Phase 2 PR 2 surfaced the file-size cap
again and motivated extraction.

ISSUE-0055 notes: the interaction-counter upsert drains its RETURNING
row with ``execute_fetchall`` rather than ``execute()`` + a separate
``fetchone()`` — the former is one aiosqlite round-trip with no
suspended VDBE.  Stepping a RETURNING statement only to its first row
leaves the *write* VDBE active across the ``await``; on the shared
connection a concurrent ``COMMIT`` in that gap previously raised
"cannot commit transaction - SQL statements in progress".
"""

from __future__ import annotations

import time

import aiosqlite

__all__ = [
    "get_interaction_count",
    "increment_interaction_count",
    "load_agent_state",
    "persist_agent_state",
    "reset_interaction_count",
]


# ─── Interaction counter helpers ─────────────────────────────


async def get_interaction_count(db: aiosqlite.Connection, agent_id: str) -> int:
    """Get the current interaction count for this agent."""
    async with db.execute(
        "SELECT interaction_count FROM agent_state WHERE agent_id = ?",
        (agent_id,),
    ) as cursor:
        row = await cursor.fetchone()
    return row[0] if row else 0


async def increment_interaction_count(
    db: aiosqlite.Connection, agent_id: str,
) -> int:
    """Increment and return the new interaction count (upsert).

    Uses RETURNING + ``execute_fetchall`` so the write VDBE never
    suspends across an ``await`` (ISSUE-0055; see module docstring).
    Requires SQLite >= 3.35 (Python 3.11+ ships >= 3.39).
    """
    now = time.time()
    rows = list(await db.execute_fetchall(
        """
        INSERT INTO agent_state (agent_id, interaction_count, updated_at)
        VALUES (?, 1, ?)
        ON CONFLICT(agent_id) DO UPDATE
            SET interaction_count = interaction_count + 1,
                updated_at = ?
        RETURNING interaction_count
        """,
        (agent_id, now, now),
    ))
    await db.commit()
    return rows[0][0] if rows else 0


async def reset_interaction_count(
    db: aiosqlite.Connection, agent_id: str,
) -> None:
    """Reset the interaction counter to zero."""
    now = time.time()
    await db.execute(
        """
        INSERT INTO agent_state (agent_id, interaction_count, updated_at)
        VALUES (?, 0, ?)
        ON CONFLICT(agent_id) DO UPDATE
            SET interaction_count = 0,
                updated_at = ?
        """,
        (agent_id, now, now),
    )
    await db.commit()


# ─── Persona state persistence helpers ──────────────────────


async def persist_agent_state(
    db: aiosqlite.Connection,
    agent_id: str,
    state_json: str,
) -> None:
    """Persist opaque agent state JSON to the agent_state table (upsert).

    Preserves interaction_count managed by the interaction counter helpers.
    """
    now = time.time()
    await db.execute(
        """
        INSERT INTO agent_state
            (agent_id, interaction_count, persona_state_json, updated_at)
        VALUES (?, 0, ?, ?)
        ON CONFLICT(agent_id) DO UPDATE
            SET persona_state_json = ?,
                updated_at = ?
        """,
        (agent_id, state_json, now, state_json, now),
    )
    await db.commit()


async def load_agent_state(
    db: aiosqlite.Connection,
    agent_id: str,
) -> str | None:
    """Load opaque agent state JSON from the agent_state table.

    Returns ``None`` if no state has been persisted for this agent.
    """
    async with db.execute(
        "SELECT persona_state_json FROM agent_state WHERE agent_id = ?",
        (agent_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row and row[0]:
        result: str = row[0]
        return result
    return None
