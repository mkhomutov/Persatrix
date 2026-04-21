"""
SQL query helpers and Episode data model for EpisodicMemory.

Contains the Episode dataclass, column constants, row conversion, recall
query implementations (FTS5 and LIKE fallback), and agent-state helpers
(interaction counter, persona-state persistence).

All functions accept an open ``aiosqlite.Connection`` and an ``agent_id``
string; they carry no object state and are safe to call from any async context.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

import aiosqlite

from .migrations import _SCORE_EXPR, _SCORE_EXPR_BARE

logger = logging.getLogger(__name__)

__all__ = [
    "Episode",
    "EPISODE_SELECT",
    "MAX_RECALL_LIMIT",
    "row_to_episode",
    "recall_fts5",
    "recall_like",
    "recall_recency",
    "get_interaction_count",
    "increment_interaction_count",
    "reset_interaction_count",
    "persist_agent_state",
    "load_agent_state",
    "_normalize_bm25",
]


# ─── Data model ─────────────────────────────────────────────


@dataclass
class Episode:
    """A single episodic memory entry."""

    id: str
    agent_id: str
    summary: str
    context: dict[str, Any]
    outcome: str | None
    importance: float
    access_count: int
    last_accessed_at: float | None
    tags: list[str]
    created_at: float
    compressed_at: float | None
    compression_level: int  # 0=raw, 1=summarized, 2=distilled


# Column list for SELECT queries — keeps row_to_episode() positional
# mapping stable when future migrations add columns to the episodes table.
_EPISODE_COLS = (
    "id", "agent_id", "summary", "context_json", "outcome",
    "importance", "access_count", "last_accessed_at",
    "tags_json", "created_at", "compressed_at", "compression_level",
)
EPISODE_SELECT = ", ".join(_EPISODE_COLS)
_EPISODE_SELECT_ALIASED = ", ".join(f"e.{c}" for c in _EPISODE_COLS)

# Maximum number of episodes returned by recall() to prevent unbounded
# result sets and resource exhaustion.
MAX_RECALL_LIMIT = 100

# FTS5 query sanitizer: strip all non-alphanumeric characters except spaces
# to prevent syntax errors from punctuation in natural language queries
# (commas, periods, colons, angle brackets, pipes, etc.).
_FTS5_SANITIZE = re.compile(r'[^a-zA-Z0-9\s]+')


# ─── Row conversion ─────────────────────────────────────────


def row_to_episode(row: aiosqlite.Row) -> Episode:
    """Convert a database row to an Episode dataclass."""
    return Episode(
        id=row[0],
        agent_id=row[1],
        summary=row[2],
        context=json.loads(row[3]) if row[3] else {},
        outcome=row[4],
        importance=row[5],
        access_count=row[6],
        last_accessed_at=row[7],
        tags=json.loads(row[8]) if row[8] else [],
        created_at=row[9],
        compressed_at=row[10],
        compression_level=row[11],
    )


# ─── Recall query helpers ────────────────────────────────────


def _normalize_bm25(raw: float | None) -> float:
    """Normalise an FTS5 BM25 raw score into [0, 1].

    FTS5 returns negative BM25 scores where more-negative means more relevant.
    Mapping: ``1.0 / (1.0 + abs(raw))``.

    Returns ``0.0`` for ``None`` or ``0.0`` input (no match signal).
    The result is clamped to ``[0.0, 1.0]``.
    """
    if not raw:
        return 0.0
    return min(1.0, max(0.0, 1.0 / (1.0 + abs(raw))))


async def recall_fts5(
    db: aiosqlite.Connection,
    agent_id: str,
    query: str,
    limit: int,
    min_importance: float,
    min_score: float | None = None,
) -> list[aiosqlite.Row]:
    """FTS5 search with composite BM25 x importance x access x recency scoring.

    Falls back to LIKE search if the query contains malformed FTS5 syntax
    (e.g. lone ``*``, unbalanced quotes, bare ``NOT``).
    """
    safe_query = _FTS5_SANITIZE.sub(" ", query).strip()
    if not safe_query:
        # Pure-punctuation query (e.g. ".,<>|!@#") sanitizes to empty.
        # LIKE on the raw query would match literally on punctuation —
        # rarely useful and surprising.  Fall through to a pure recency/
        # importance ranking so the caller still gets relevant episodes.
        return await recall_recency(db, agent_id, limit, min_importance)
    # Normalised BM25 floor: 1.0/(1+|rank|) >= min_score  iff  |rank| <= (1/min_score - 1).
    # Passing 0.0 when min_score is None/0.0 lets every match through.
    effective_min_score = min_score if min_score is not None else 0.0
    try:
        async with db.execute(
            f"""
            SELECT {_EPISODE_SELECT_ALIASED}
            FROM episodes_fts fts
            JOIN episodes e ON e.rowid = fts.rowid
            WHERE episodes_fts MATCH ?
              AND e.agent_id = ?
              AND e.importance >= ?
              AND (1.0 / (1.0 + ABS(fts.rank))) >= ?
            ORDER BY
                (fts.rank * -1)
                * {_SCORE_EXPR}
                DESC
            LIMIT ?
            """,
            (safe_query, agent_id, min_importance, effective_min_score, time.time(), limit),
        ) as cursor:
            return list(await cursor.fetchall())
    except sqlite3.OperationalError as exc:
        logger.warning(
            "FTS5 query failed for %r, falling back to LIKE: %s", query, exc,
        )
        return await recall_like(db, agent_id, query, limit, min_importance, min_score)


async def recall_like(
    db: aiosqlite.Connection,
    agent_id: str,
    query: str,
    limit: int,
    min_importance: float,
    min_score: float | None = None,  # noqa: ARG001 — LIKE matches are binary (score=1.0)
) -> list[aiosqlite.Row]:
    """LIKE fallback when FTS5 is unavailable.

    Escapes LIKE wildcard characters (``%``, ``_``) in the query so they
    are matched literally rather than treated as pattern metacharacters.

    ``min_score`` is accepted for signature compatibility but is not applied:
    LIKE matching is binary (match or not), so every match is treated as
    score ``1.0`` per RFC 0017 Section C.
    """
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped}%"
    async with db.execute(
        f"""
        SELECT {EPISODE_SELECT}
        FROM episodes
        WHERE agent_id = ?
          AND importance >= ?
          AND (summary LIKE ? ESCAPE '\\' OR context_json LIKE ? ESCAPE '\\')
        ORDER BY
            {_SCORE_EXPR_BARE}
            DESC
        LIMIT ?
        """,
        (agent_id, min_importance, pattern, pattern, time.time(), limit),
    ) as cursor:
        return list(await cursor.fetchall())


async def recall_recency(
    db: aiosqlite.Connection,
    agent_id: str,
    limit: int,
    min_importance: float,
) -> list[aiosqlite.Row]:
    """No query text — rank by importance x access x recency only."""
    async with db.execute(
        f"""
        SELECT {EPISODE_SELECT}
        FROM episodes
        WHERE agent_id = ?
          AND importance >= ?
        ORDER BY
            {_SCORE_EXPR_BARE}
            DESC
        LIMIT ?
        """,
        (agent_id, min_importance, time.time(), limit),
    ) as cursor:
        return list(await cursor.fetchall())


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

    Uses RETURNING to get the post-upsert count in a single round-trip,
    eliminating a read-after-write race (F-3b-2).  Requires SQLite >= 3.35
    (Python 3.11+ ships >= 3.39).
    """
    now = time.time()
    cursor = await db.execute(
        """
        INSERT INTO agent_state (agent_id, interaction_count, updated_at)
        VALUES (?, 1, ?)
        ON CONFLICT(agent_id) DO UPDATE
            SET interaction_count = interaction_count + 1,
                updated_at = ?
        RETURNING interaction_count
        """,
        (agent_id, now, now),
    )
    row = await cursor.fetchone()
    await db.commit()
    return row[0] if row else 0


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
