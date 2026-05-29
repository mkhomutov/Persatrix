"""Notes-tier recall helpers extracted from :mod:`agents.memory.notes`.

Split out so the parent module stays under the project's 500-line
review-friendly cap (see ``scripts/checks/file_size.py``).  Three free
functions — :func:`_recall_notes_fts5` / :func:`_recall_notes_like` /
:func:`_recall_notes_recency` — accept the live ``aiosqlite``
connection plus the agent's ``agent_id`` and the resolved session list,
returning raw rows.  :class:`agents.memory.notes.NoteStore` is the
public entry point; these are internal helpers and not exported.
"""

from __future__ import annotations

import logging
import re
import sqlite3

import aiosqlite

from ._session_filter import session_in_clause
from .episodic_queries import resolve_min_score

logger = logging.getLogger(__name__)

# FTS5 MATCH operator characters that cause parse errors when present in
# freeform queries — strip all non-alphanumeric characters except spaces.
_FTS5_SPECIAL = re.compile(r'[^a-zA-Z0-9\s]+')


async def _recall_notes_fts5(
    db: aiosqlite.Connection,
    *,
    agent_id: str,
    query: str,
    limit: int,
    min_score: float | None,
    sessions: list[str] | None,
    note_cols: tuple[str, ...],
) -> list[aiosqlite.Row]:
    """FTS5 search across topic, content, and tags.

    ``sessions`` is the resolved session list from
    :func:`agents.memory._session_filter._resolve_session_list`;
    ``None`` is the ``"*"`` no-filter mode.  Falls back to LIKE on
    FTS5 parse failure or empty sanitized query.
    """
    sess_clause, sess_params = session_in_clause(
        sessions, column="n.session_id",
    )
    safe_query = _FTS5_SPECIAL.sub(" ", query).strip()
    if not safe_query:
        return await _recall_notes_like(
            db, agent_id=agent_id, query=query, limit=limit,
            min_score=min_score, sessions=sessions, note_cols=note_cols,
        )
    effective_min_score = resolve_min_score(min_score)
    try:
        async with db.execute(
            f"""
            SELECT {", ".join(f"n.{c}" for c in note_cols)}
            FROM notes_fts fts
            JOIN notes n ON n.rowid = fts.rowid
            WHERE notes_fts MATCH ?
              AND n.agent_id = ?
              AND (1.0 / (1.0 + ABS(fts.rank))) >= ?
              {sess_clause}
            ORDER BY fts.rank * -1 DESC
            LIMIT ?
            """,
            (
                safe_query, agent_id, effective_min_score,
                *sess_params, limit,
            ),
        ) as cursor:
            return list(await cursor.fetchall())
    except sqlite3.OperationalError as exc:
        logger.warning(
            "Notes FTS5 query failed for %r (sanitized: %r), falling back to LIKE: %s",
            query, safe_query, exc,
        )
        return await _recall_notes_like(
            db, agent_id=agent_id, query=query, limit=limit,
            min_score=min_score, sessions=sessions, note_cols=note_cols,
        )


async def _recall_notes_like(
    db: aiosqlite.Connection,
    *,
    agent_id: str,
    query: str,
    limit: int,
    min_score: float | None,  # noqa: ARG001 — LIKE matches score 1.0
    sessions: list[str] | None,
    note_cols: tuple[str, ...],
) -> list[aiosqlite.Row]:
    """LIKE fallback when FTS5 is unavailable.

    ``min_score`` is accepted for signature compatibility but not
    applied: LIKE matching is binary so every match scores ``1.0`` per
    RFC 0017 Section C.
    """
    sess_clause, sess_params = session_in_clause(
        sessions, column="session_id",
    )
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped}%"
    note_select = ", ".join(note_cols)
    async with db.execute(
        f"""
        SELECT {note_select}
        FROM notes
        WHERE agent_id = ?
          AND (topic LIKE ? ESCAPE '\\'
               OR content LIKE ? ESCAPE '\\'
               OR tags_json LIKE ? ESCAPE '\\')
          {sess_clause}
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        (agent_id, pattern, pattern, pattern, *sess_params, limit),
    ) as cursor:
        return list(await cursor.fetchall())


async def _recall_notes_recency(
    db: aiosqlite.Connection,
    *,
    agent_id: str,
    limit: int,
    sessions: list[str] | None,
    note_cols: tuple[str, ...],
) -> list[aiosqlite.Row]:
    """No query — return most recently updated notes."""
    sess_clause, sess_params = session_in_clause(
        sessions, column="session_id",
    )
    note_select = ", ".join(note_cols)
    async with db.execute(
        f"""
        SELECT {note_select}
        FROM notes
        WHERE agent_id = ?
          {sess_clause}
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        (agent_id, *sess_params, limit),
    ) as cursor:
        return list(await cursor.fetchall())
