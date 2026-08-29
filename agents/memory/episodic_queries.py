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
from typing import Any

import aiosqlite

from ..epoch_id import DEFAULT_EPOCH_ID
from ..principal_id import DEFAULT_PRINCIPAL_ID
from ._episodic_agent_state import (
    get_interaction_count,
    increment_interaction_count,
    load_agent_state,
    persist_agent_state,
    reset_interaction_count,
)
from ._epoch_filter import epoch_eq_clause
from ._migration_protection import PROTECTION_LEVEL_DEFAULT
from ._principal_filter import principal_eq_clause
from ._session_filter import session_boost_expr, session_in_clause
from .episode_types import (
    _EPISODE_SELECT_ALIASED,
    EPISODE_SELECT,
    Episode,
    row_to_episode,
)
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
# ``Episode`` / ``EPISODE_SELECT`` / ``row_to_episode`` moved to
# :mod:`agents.memory.episode_types` (RFC 0037 PR 4 — 500-line cap);
# re-exported above so every existing import keeps working.

# Maximum number of episodes returned by recall() to prevent unbounded
# result sets and resource exhaustion.
MAX_RECALL_LIMIT = 100

# FTS5 query sanitizer: strip all non-alphanumeric characters except spaces
# to prevent syntax errors from punctuation in natural language queries
# (commas, periods, colons, angle brackets, pipes, etc.).
_FTS5_SANITIZE = re.compile(r'[^a-zA-Z0-9\s]+')


# ─── Recall query helpers ────────────────────────────────────


def _reject_wall_and_boost(
    sessions: list[str] | None,
    boost_sessions: list[str] | None,
) -> None:
    """Enforce the either-wall-or-boost contract (RFC 0049 L1 amendment).

    A caller supplies the resolved session list as the WHERE wall
    (``sessions``) or as the ranking boost (``boost_sessions``), never
    both — boosting a subset of an already-filtered set silently
    re-creates the wall the ranked mode exists to drop.  Originally the
    contract was held only by ``recall_room_ranked`` being the sole
    boost caller; PR 4's live-prompt promotion made that path routine,
    so the three query helpers now refuse the combination themselves
    (the #783 review follow-up).
    """
    if sessions is not None and boost_sessions:
        raise ValueError(
            "sessions and boost_sessions are mutually exclusive — pass the "
            "resolved room list as the WHERE wall or as the ranking boost, "
            "never both (RFC 0049 L1 either-wall-or-boost)",
        )


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
    boost_sessions: list[str] | None = None,
    principal_id: str = DEFAULT_PRINCIPAL_ID,
    epoch_id: str = DEFAULT_EPOCH_ID,
) -> list[aiosqlite.Row]:
    """FTS5 search with composite BM25 x importance x access x recency scoring.

    Falls back to LIKE search on malformed FTS5 syntax (lone ``*``,
    unbalanced quotes, bare ``NOT``).  ``sessions`` (RFC 0031 Phase 2
    PR 2) is a resolved list from
    :func:`agents.memory._session_filter._resolve_session_list` — ``None``
    is the ``"*"`` no-filter mode.

    ``boost_sessions`` (RFC 0049 L1 amendment) applies the room-first
    ranking multiplier (:func:`session_boost_expr`) to the composite
    score instead of a WHERE wall — pass it with ``sessions=None``; the
    either-wall-or-boost contract is enforced by
    :func:`agents.memory.episodic_room_ranked.recall_room_ranked`.

    ``principal_id`` (ISSUE-0081 PR 3) is the resolved active tenant; the
    predicate is unconditional strict equality (no carve-out, no
    no-filter mode).  Defaults to :data:`DEFAULT_PRINCIPAL_ID` so a
    single-tenant direct caller is fail-safe; the production tier always
    passes the call-time-resolved active principal.

    ``epoch_id`` (ISSUE-0085 PR 3) is the resolved active run/test epoch;
    same unconditional strict-equality shape as ``principal_id`` (no
    carve-out, no ``"*"`` bypass).  Defaults to :data:`DEFAULT_EPOCH_ID`
    so a single-world direct caller is fail-safe.
    """
    _reject_wall_and_boost(sessions, boost_sessions)
    sess_clause, sess_params = session_in_clause(sessions, column="e.session_id")
    boost_expr, boost_params = session_boost_expr(
        boost_sessions, column="e.session_id",
    )
    princ_clause, princ_params = principal_eq_clause(
        principal_id, column="e.principal_id",
    )
    epoch_clause, epoch_params = epoch_eq_clause(
        epoch_id, column="e.epoch_id",
    )
    safe_query = _FTS5_SANITIZE.sub(" ", query).strip()
    if not safe_query:
        # Pure-punctuation query sanitizes to empty — fall through to a
        # pure recency ranking so the caller still gets relevant rows.
        return await recall_recency(
            db, agent_id, limit, min_importance, sessions=sessions,
            boost_sessions=boost_sessions,
            principal_id=principal_id, epoch_id=epoch_id,
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
              {epoch_clause}
            ORDER BY
                (fts.rank * -1)
                * {_SCORE_EXPR}{boost_expr}
                DESC
            LIMIT ?
            """,
            (
                safe_query, agent_id, min_importance, effective_min_score,
                *sess_params, *princ_params, *epoch_params, time.time(),
                *boost_params, limit,
            ),
        ) as cursor:
            return list(await cursor.fetchall())
    except sqlite3.OperationalError as exc:
        logger.warning(
            "FTS5 query failed for %r, falling back to LIKE: %s", query, exc,
        )
        return await recall_like(
            db, agent_id, query, limit, min_importance, min_score,
            sessions=sessions, boost_sessions=boost_sessions,
            principal_id=principal_id, epoch_id=epoch_id,
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
    boost_sessions: list[str] | None = None,
    principal_id: str = DEFAULT_PRINCIPAL_ID,
    epoch_id: str = DEFAULT_EPOCH_ID,
) -> list[aiosqlite.Row]:
    """LIKE fallback when FTS5 is unavailable.

    Escapes ``%`` / ``_`` so they match literally.  ``min_score`` is
    signature-only — LIKE matches are binary, every match scores ``1.0``
    per RFC 0017 §C.  ``sessions`` / ``boost_sessions`` /
    ``principal_id`` / ``epoch_id`` — see :func:`recall_fts5`.
    """
    _reject_wall_and_boost(sessions, boost_sessions)
    sess_clause, sess_params = session_in_clause(sessions, column="session_id")
    boost_expr, boost_params = session_boost_expr(
        boost_sessions, column="session_id",
    )
    princ_clause, princ_params = principal_eq_clause(
        principal_id, column="principal_id",
    )
    epoch_clause, epoch_params = epoch_eq_clause(
        epoch_id, column="epoch_id",
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
          {epoch_clause}
        ORDER BY
            {_SCORE_EXPR_BARE}{boost_expr}
            DESC
        LIMIT ?
        """,
        (
            agent_id, min_importance, pattern, pattern,
            *sess_params, *princ_params, *epoch_params, time.time(),
            *boost_params, limit,
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
    boost_sessions: list[str] | None = None,
    principal_id: str = DEFAULT_PRINCIPAL_ID,
    epoch_id: str = DEFAULT_EPOCH_ID,
) -> list[aiosqlite.Row]:
    """No query text — rank by importance x access x recency only.

    ``sessions`` / ``boost_sessions`` / ``principal_id`` / ``epoch_id``
    — see :func:`recall_fts5`.  The persona-runtime channel-history tier
    hits this path with an empty query, so this is the load-bearing path
    for closing F-3 (and the ISSUE-0081 / ISSUE-0085 cross-axis leaks)
    on the empty-query surface.
    """
    _reject_wall_and_boost(sessions, boost_sessions)
    sess_clause, sess_params = session_in_clause(sessions, column="session_id")
    boost_expr, boost_params = session_boost_expr(
        boost_sessions, column="session_id",
    )
    princ_clause, princ_params = principal_eq_clause(
        principal_id, column="principal_id",
    )
    epoch_clause, epoch_params = epoch_eq_clause(
        epoch_id, column="epoch_id",
    )
    async with db.execute(
        f"""
        SELECT {EPISODE_SELECT}
        FROM episodes
        WHERE agent_id = ?
          AND importance >= ?
          {sess_clause}
          {princ_clause}
          {epoch_clause}
        ORDER BY
            {_SCORE_EXPR_BARE}{boost_expr}
            DESC
        LIMIT ?
        """,
        (
            agent_id, min_importance, *sess_params, *princ_params,
            *epoch_params, time.time(), *boost_params, limit,
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
    governance_interaction_id: str | None = None,
    principal_id: str = DEFAULT_PRINCIPAL_ID,
    epoch_id: str = DEFAULT_EPOCH_ID,
    protection_level: str = PROTECTION_LEVEL_DEFAULT,
    source_channel_id: str | None = None,
    speaker_id: str | None = None,
) -> str:
    """INSERT one episode row and COMMIT; return the generated episode id.

    Extracted from :meth:`EpisodicMemory.store_episode` so the episode
    INSERT sits in :mod:`agents.memory.episodic_queries` beside the
    sibling write helper :func:`update_episode_summary` that already
    owns the episode SQL, keeping ``episodic.py`` within the 500-line
    file-size cap.

    ``protection_level`` / ``source_channel_id`` (RFC 0037 §C — v16)
    persist verbatim; see :meth:`EpisodicMemory.store_episode` for the
    stamp-site normalization contract.

    ``speaker_id`` (ISSUE-0131 — v18) is the PROJECTION of the
    ``(principal, speaker, scope)`` record key's speaker half: WHO said
    the content this row was derived from.  ``None`` for a row with no
    speaker (a tick, a single-turn scope, an operator/test write) and
    for every pre-v18 row, whose speaker is genuinely unknowable — the
    aggregate it came from spanned the whole room, which is the defect
    the key exists to fix.  Sound only because a record is single-speaker
    by construction; the ONE exception, the RFC 0020 §G room-close turn,
    is excluded upstream from the derivation input rather than corrected
    here.

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
             session_id, principal_id, epoch_id, governance_interaction_id,
             protection_level, source_channel_id, speaker_id)
        VALUES (?, ?, ?, ?, ?, ?, 0, NULL, ?, ?, NULL, 0,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?)
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
            epoch_id,
            governance_interaction_id,
            protection_level,
            source_channel_id,
            speaker_id,
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
