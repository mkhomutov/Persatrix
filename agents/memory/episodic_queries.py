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
import uuid
from dataclasses import dataclass
from typing import Any

import aiosqlite

from ..principal_id import DEFAULT_PRINCIPAL_ID
from ._episodic_agent_state import (
    get_interaction_count,
    increment_interaction_count,
    load_agent_state,
    persist_agent_state,
    reset_interaction_count,
)
from ._principal_filter import principal_eq_clause
from ._session_filter import session_in_clause
from .interaction_janitor import SUMMARY_PENDING_TEXT
from .migrations import _SCORE_EXPR, _SCORE_EXPR_BARE

logger = logging.getLogger(__name__)

__all__ = [
    "Episode",
    "EPISODE_SELECT",
    "MAX_RECALL_LIMIT",
    "row_to_episode",
    "insert_episode",
    "recall_fts5",
    "recall_like",
    "recall_recency",
    "get_interaction_count",
    "increment_interaction_count",
    "reset_interaction_count",
    "persist_agent_state",
    "load_agent_state",
    # `_normalize_bm25` is exported for testability (RFC 0017 §C); the
    # underscore prefix marks it as not part of the stable public API.
    # PR #147 review: documented to resolve `_`-prefix vs `__all__` tension.
    "_normalize_bm25",
    # `resolve_min_score` is imported cross-module by `notes.py` (production
    # code, not just tests), so unlike `_normalize_bm25` it is promoted to a
    # public name. Mirrors the same-PR promotion of `DEFAULT_*_MIN_SCORE`:
    # if it crosses a module boundary in production, it does not get an
    # underscore. (PR 6 — RFC 0017 PR 6 review finding: rename helper.)
    "resolve_min_score",
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
    # RFC 0020 §D column-level scope, projected onto the dataclass in
    # PR 2a so non-facade writers (``InteractionTracker``) are visible to
    # ``MemoryStore.retrieve_relevant(scope=...)`` filtering.  Defaults
    # to ``None`` so legacy rows without a scope value round-trip cleanly.
    scope: str | None = None
    # RFC 0021 PR 2 (this PR): expose the RFC 0020 §D interaction columns
    # to recall consumers so the persona-runtime memory packaging path can
    # render duration prefixes ("over 47 min, with Bob") on multi-turn
    # episodes.  All four default to ``None`` so legacy / single-turn rows
    # round-trip cleanly — recency rendering falls back to ``created_at``
    # when ``closed_at`` is missing.
    interaction_id: str | None = None
    started_at: float | None = None
    closed_at: float | None = None
    turn_count: int | None = None


# Column list for SELECT queries — keeps row_to_episode() positional
# mapping stable when future migrations add columns to the episodes table.
_EPISODE_COLS = (
    "id", "agent_id", "summary", "context_json", "outcome",
    "importance", "access_count", "last_accessed_at",
    "tags_json", "created_at", "compressed_at", "compression_level",
    "scope",
    # RFC 0020 §D + RFC 0021 PR 2: interaction columns surfaced to recall.
    "interaction_id", "started_at", "closed_at", "turn_count",
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
        scope=row[12],
        # RFC 0020 §D / RFC 0021 PR 2: trailing optional columns.  Legacy
        # rows that pre-date RFC 0020 PR 1 carry ``NULL`` here.
        interaction_id=row[13],
        started_at=row[14],
        closed_at=row[15],
        turn_count=row[16],
    )


# ─── Recall query helpers ────────────────────────────────────


def _normalize_bm25(raw: float | None) -> float:
    """Normalise an FTS5 BM25 raw score into [0, 1].

    FTS5 returns negative BM25 scores where more-negative means more relevant.
    Mapping: ``1.0 / (1.0 + abs(raw))``.

    Returns ``0.0`` for ``None`` or ``0.0`` input (no match signal).
    The result is clamped to ``[0.0, 1.0]``.

    Notes
    -----
    For ``raw == 0.0`` this helper returns ``0.0`` (treated as no-match),
    while the equivalent SQL expression in :func:`recall_fts5`
    (``1.0 / (1.0 + ABS(rank))``) would compute ``1.0`` for the same input.
    In practice FTS5 never returns ``rank = 0.0`` for a MATCH row, so the
    divergence has no operational impact — but callers using this helper
    to predict SQL threshold outcomes should be aware of the edge case.
    (PR #147 review.)
    """
    if not raw:
        return 0.0
    return min(1.0, max(0.0, 1.0 / (1.0 + abs(raw))))


def resolve_min_score(min_score: float | None) -> float:
    """Resolve ``None`` to ``0.0`` for SQL-side BM25 floor parameters.

    ``None`` means "no SQL-side filter": passing ``0.0`` to the
    ``(1.0/(1.0+ABS(rank))) >= ?`` predicate lets every match through
    because the normalised score is always in ``(0, 1]`` for non-zero
    ranks.  Centralised here so the contract is a single line of truth
    shared by :func:`recall_fts5` and ``NoteStore._recall_notes_fts5``.
    (PR 6 — RFC 0017 PR 3 review finding 3.)

    Note: kept underscore-free (unlike :func:`_normalize_bm25`) because it
    is imported cross-module by :mod:`agents.memory.notes` in production
    code, not just tests.  See ``__all__`` rationale above.
    """
    return 0.0 if min_score is None else min_score


async def recall_fts5(
    db: aiosqlite.Connection,
    agent_id: str,
    query: str,
    limit: int,
    min_importance: float,
    min_score: float | None = None,
    *,
    sessions: list[str] | None = None,
    principal_id: str = DEFAULT_PRINCIPAL_ID,
) -> list[aiosqlite.Row]:
    """FTS5 search with composite BM25 x importance x access x recency scoring.

    Falls back to LIKE search on malformed FTS5 syntax (lone ``*``,
    unbalanced quotes, bare ``NOT``).  ``sessions`` (RFC 0031 Phase 2
    PR 2) is a resolved list from
    :func:`agents.memory._session_filter._resolve_session_list` — ``None``
    is the ``"*"`` no-filter mode.

    ``principal_id`` (ISSUE-0081 PR 3) is the resolved active tenant; the
    predicate is unconditional strict equality (no carve-out, no
    no-filter mode).  Defaults to :data:`DEFAULT_PRINCIPAL_ID` so a
    single-tenant direct caller is fail-safe; the production tier always
    passes the call-time-resolved active principal.
    """
    sess_clause, sess_params = session_in_clause(sessions, column="e.session_id")
    princ_clause, princ_params = principal_eq_clause(
        principal_id, column="e.principal_id",
    )
    safe_query = _FTS5_SANITIZE.sub(" ", query).strip()
    if not safe_query:
        # Pure-punctuation query sanitizes to empty — fall through to a
        # pure recency ranking so the caller still gets relevant rows.
        return await recall_recency(
            db, agent_id, limit, min_importance, sessions=sessions,
            principal_id=principal_id,
        )
    effective_min_score = resolve_min_score(min_score)
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
              {sess_clause}
              {princ_clause}
            ORDER BY
                (fts.rank * -1)
                * {_SCORE_EXPR}
                DESC
            LIMIT ?
            """,
            (
                safe_query, agent_id, min_importance, effective_min_score,
                *sess_params, *princ_params, time.time(), limit,
            ),
        ) as cursor:
            return list(await cursor.fetchall())
    except sqlite3.OperationalError as exc:
        logger.warning(
            "FTS5 query failed for %r, falling back to LIKE: %s", query, exc,
        )
        return await recall_like(
            db, agent_id, query, limit, min_importance, min_score,
            sessions=sessions, principal_id=principal_id,
        )


async def recall_like(
    db: aiosqlite.Connection,
    agent_id: str,
    query: str,
    limit: int,
    min_importance: float,
    min_score: float | None = None,  # noqa: ARG001 — LIKE matches are binary (score=1.0)
    *,
    sessions: list[str] | None = None,
    principal_id: str = DEFAULT_PRINCIPAL_ID,
) -> list[aiosqlite.Row]:
    """LIKE fallback when FTS5 is unavailable.

    Escapes ``%`` / ``_`` so they match literally.  ``min_score`` is
    signature-only — LIKE matches are binary, every match scores ``1.0``
    per RFC 0017 §C.  ``sessions`` / ``principal_id`` — see
    :func:`recall_fts5`.
    """
    sess_clause, sess_params = session_in_clause(sessions, column="session_id")
    princ_clause, princ_params = principal_eq_clause(
        principal_id, column="principal_id",
    )
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped}%"
    async with db.execute(
        f"""
        SELECT {EPISODE_SELECT}
        FROM episodes
        WHERE agent_id = ?
          AND importance >= ?
          AND (summary LIKE ? ESCAPE '\\' OR context_json LIKE ? ESCAPE '\\')
          {sess_clause}
          {princ_clause}
        ORDER BY
            {_SCORE_EXPR_BARE}
            DESC
        LIMIT ?
        """,
        (
            agent_id, min_importance, pattern, pattern,
            *sess_params, *princ_params, time.time(), limit,
        ),
    ) as cursor:
        return list(await cursor.fetchall())


async def recall_recency(
    db: aiosqlite.Connection,
    agent_id: str,
    limit: int,
    min_importance: float,
    *,
    sessions: list[str] | None = None,
    principal_id: str = DEFAULT_PRINCIPAL_ID,
) -> list[aiosqlite.Row]:
    """No query text — rank by importance x access x recency only.

    ``sessions`` / ``principal_id`` — see :func:`recall_fts5`.  The
    persona-runtime channel-history tier hits this path with an empty
    query, so this is the load-bearing path for closing F-3 (and the
    ISSUE-0081 cross-tenant leak) on the empty-query surface.
    """
    sess_clause, sess_params = session_in_clause(sessions, column="session_id")
    princ_clause, princ_params = principal_eq_clause(
        principal_id, column="principal_id",
    )
    async with db.execute(
        f"""
        SELECT {EPISODE_SELECT}
        FROM episodes
        WHERE agent_id = ?
          AND importance >= ?
          {sess_clause}
          {princ_clause}
        ORDER BY
            {_SCORE_EXPR_BARE}
            DESC
        LIMIT ?
        """,
        (
            agent_id, min_importance, *sess_params, *princ_params,
            time.time(), limit,
        ),
    ) as cursor:
        return list(await cursor.fetchall())


# ─── Episode write helpers ──────────────────────────────────


async def insert_episode(
    db: aiosqlite.Connection,
    agent_id: str,
    *,
    summary: str,
    context: dict[str, Any],
    outcome: str | None,
    importance: float,
    tags: list[str] | None,
    interaction_id: str | None,
    started_at: float | None,
    closed_at: float | None,
    turn_count: int | None,
    session_id: str,
    scope: str | None,
    principal_id: str = DEFAULT_PRINCIPAL_ID,
) -> str:
    """INSERT one episode row and COMMIT; return the generated episode id.

    Extracted from :meth:`EpisodicMemory.store_episode` so the episode
    INSERT sits in :mod:`agents.memory.episodic_queries` beside the
    sibling write helper :func:`update_episode_summary` that already
    owns the episode SQL, keeping ``episodic.py`` within the 500-line
    file-size cap.

    The INSERT is plain DML — stepped to completion inside ``execute()``
    with no VDBE left active — so a concurrent ``COMMIT`` on the shared
    connection cannot race it (ISSUE-0055).
    """
    episode_id = str(uuid.uuid4())
    now = time.time()
    await db.execute(
        """
        INSERT INTO episodes
            (id, agent_id, summary, context_json, outcome,
             importance, access_count, last_accessed_at,
             tags_json, created_at, compressed_at, compression_level,
             interaction_id, started_at, closed_at, turn_count, scope,
             session_id, principal_id)
        VALUES (?, ?, ?, ?, ?, ?, 0, NULL, ?, ?, NULL, 0,
                ?, ?, ?, ?, ?,
                ?, ?)
        """,
        (
            episode_id,
            agent_id,
            summary,
            json.dumps(context),
            outcome,
            importance,
            json.dumps(tags or []),
            now,
            interaction_id,
            started_at,
            closed_at,
            turn_count,
            scope,
            session_id,
            principal_id,
        ),
    )
    await db.commit()
    return episode_id


async def update_episode_summary(
    db: aiosqlite.Connection, agent_id: str,
    interaction_id: str, summary: str,
) -> bool:
    """Replace ``[summary pending]`` for an episode (RFC 0020 PR 4 close-path).

    Agent-scoped UPDATE that *only* matches rows still carrying the
    :data:`SUMMARY_PENDING_TEXT` sentinel — the janitor's
    :data:`SUMMARY_UNAVAILABLE_TEXT` verdict must be final once written
    so a late-successful Phase-2 LLM completion cannot overwrite it
    (PR 6 review #20).  Returns ``True`` iff this UPDATE replaced a
    pending row; callers use a ``False`` return to skip the
    relationship-bump and auto-reflect tick so the failure counter
    cannot double-increment for the same interaction.

    The single-writer invariant guarantees ``summary`` is non-empty at
    every production call site (the summariser returns either LLM text
    or :data:`SUMMARY_UNAVAILABLE_TEXT`); revalidating here was dead
    code (PR 6 review #22).
    """
    cursor = await db.execute(
        "UPDATE episodes SET summary = ? "
        "WHERE agent_id = ? AND interaction_id = ? AND summary = ?",
        (summary, agent_id, interaction_id, SUMMARY_PENDING_TEXT),
    )
    await db.commit()
    return (cursor.rowcount or 0) > 0
