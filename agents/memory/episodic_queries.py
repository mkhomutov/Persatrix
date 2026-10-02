"""
SQL query helpers and Episode data model for EpisodicMemory.

Contains the Episode dataclass, column constants, row conversion, recall
query implementations (FTS5 and LIKE fallback), and agent-state helpers
(interaction counter, persona-state persistence). The episode write helpers
live in :mod:`agents.memory.episodic_writes`.

All functions accept an open ``aiosqlite.Connection`` and an ``agent_id``
string; they carry no object state and are safe to call from any async context.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence

import aiosqlite

from ..clock import agent_now
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
from ._fts5_query import EPISODE_STRUCTURAL_WORDS, FTS5_SANITIZE, fts5_match_query
from ._principal_filter import principal_eq_clause
from ._session_filter import session_boost_expr, session_in_clause
from .episode_types import (
    _EPISODE_SELECT_ALIASED,
    EPISODE_SELECT,
    Episode,
    row_to_episode,
)
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
    # `resolve_min_score` is imported cross-module by `notes.py` (production
    # code, not just tests), so it carries a public name. Mirrors the
    # same-PR promotion of `DEFAULT_*_MIN_SCORE`:
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

# The character class recall once sanitised the whole query with; the MATCH
# text is now built by :func:`~._fts5_query.fts5_match_query`. The name stays
# for the tests that pin the class.
_FTS5_SANITIZE = FTS5_SANITIZE


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


def resolve_min_score(min_score: float | None) -> float:
    """Resolve ``None`` to ``0.0`` for the SQL-side relevance floor.

    The floor keeps a row whose bm25 relevance is at least ``min_score``
    times the best relevance among the call's candidates
    (``-rank >= ? * best``). ``0.0`` lets every match through, so ``None``
    and ``0.0`` both mean no floor. Centralised here so the contract is a
    single line of truth shared by :func:`recall_fts5` and
    ``NoteStore._recall_notes_fts5``. (PR 6 — RFC 0017 PR 3 review finding 3;
    the floor became relative with ISSUE-0159.)

    Underscore-free because :mod:`agents.memory.notes` imports it in
    production code, not just tests. See the ``__all__`` rationale above.
    """
    return 0.0 if min_score is None else min_score


def _floor_bar_expr(
    floor_protection_levels: Sequence[str] | None,
) -> tuple[str, tuple[str, ...]]:
    """The best relevance the floor measures rows against, as SQL.

    With ``floor_protection_levels`` set, only candidates stored at one of
    those levels set the bar, so an episode the §D gate will withhold after
    the search cannot push an admissible one below the floor (or reveal
    that it exists by doing so). Withheld rows stay candidates: the gate's
    §E projection branch needs them. With no admissible candidate, every
    candidate sets the bar.
    """
    if floor_protection_levels is None:
        return "MAX(-fts.rank) OVER ()", ()
    placeholders = ",".join("?" for _ in floor_protection_levels)
    return (
        "COALESCE(MAX(CASE WHEN e.protection_level IN "
        f"({placeholders}) THEN -fts.rank END) OVER (), "
        "MAX(-fts.rank) OVER ())",
        tuple(floor_protection_levels),
    )


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
    floor_protection_levels: Sequence[str] | None = None,
) -> list[aiosqlite.Row]:
    """FTS5 search with composite BM25 x importance x access x recency scoring.

    The MATCH text is one quoted phrase per word of *query*, joined with
    OR (:func:`~._fts5_query.fts5_match_query`), so a natural sentence
    matches every episode that shares a content word with it (ISSUE-0159).
    The words the system writes into nearly every row
    (:data:`~._fts5_query.EPISODE_STRUCTURAL_WORDS`) are trimmed like common
    words. A query with no letter or digit falls back to
    :func:`recall_recency`, and the tick sentence returns no rows.

    ``min_score`` is relative: a row stays when its bm25 relevance is at
    least ``min_score`` times the best relevance among the candidates, the
    rows that match and pass this call's own filters (agent, importance,
    session wall, principal, epoch). ``floor_protection_levels`` narrows
    who sets that best to the rows the acting turn may inject
    (:func:`_floor_bar_expr`). The floor is applied before ``LIMIT``; the
    room boost and the composite score only order the rows. A fixed floor
    would empty small stores: FTS5 gives a word found in half the rows or
    more almost no weight, so in a store of one or two rows every bm25 is
    about 1e-6. bm25's word weights are counted over the whole FTS table,
    every agent, tenant, epoch and level included, so the filters above
    choose the candidates but not the weights.

    Falls back to LIKE on the raw query if FTS5 raises, which quoted
    phrases cannot cause. ``sessions`` (RFC 0031 Phase 2
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
    bar_expr, bar_params = _floor_bar_expr(floor_protection_levels)
    match = fts5_match_query(query, structural=EPISODE_STRUCTURAL_WORDS)
    if match is None:
        # No letter or digit to search for — fall through to a pure
        # recency ranking so the caller still gets relevant rows.
        return await recall_recency(
            db, agent_id, limit, min_importance, sessions=sessions,
            boost_sessions=boost_sessions,
            principal_id=principal_id, epoch_id=epoch_id,
        )
    if not match:
        return []
    try:
        # The inner query holds the candidates and their best relevance;
        # the outer one floors each row against that best, then ranks.
        async with db.execute(
            f"""
            SELECT {_EPISODE_SELECT_ALIASED}
            FROM (
                SELECT fts.rowid AS rid, fts.rank AS rank,
                       {bar_expr} AS best
                FROM episodes_fts fts
                JOIN episodes e ON e.rowid = fts.rowid
                WHERE episodes_fts MATCH ?
                  AND e.agent_id = ?
                  AND e.importance >= ?
                  {sess_clause}
                  {princ_clause}
                  {epoch_clause}
            ) c
            JOIN episodes e ON e.rowid = c.rid
            WHERE -c.rank >= ? * c.best
            ORDER BY
                (c.rank * -1)
                * {_SCORE_EXPR}{boost_expr}
                DESC
            LIMIT ?
            """,
            (
                *bar_params, match, agent_id, min_importance,
                *sess_params, *princ_params, *epoch_params,
                resolve_min_score(min_score), agent_now(),
                *boost_params, limit,
            ),
        ) as cursor:
            return list(await cursor.fetchall())
    except sqlite3.OperationalError as exc:
        logger.warning(
            "FTS5 query failed for %r (MATCH %r), falling back to LIKE: %s",
            query, match, exc,
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
            *sess_params, *princ_params, *epoch_params, agent_now(),
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
            *epoch_params, agent_now(), *boost_params, limit,
        ),
    ) as cursor:
        return list(await cursor.fetchall())
