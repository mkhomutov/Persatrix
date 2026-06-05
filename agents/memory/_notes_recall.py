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

from ..epoch_id import DEFAULT_EPOCH_ID
from ..principal_id import DEFAULT_PRINCIPAL_ID
from ._epoch_filter import epoch_eq_clause
from ._principal_filter import principal_eq_clause
from ._session_filter import session_in_clause
from .episodic_queries import resolve_min_score

logger = logging.getLogger(__name__)

# FTS5 MATCH operator characters that cause parse errors when present in
# freeform queries — strip all non-alphanumeric characters except spaces.
_FTS5_SPECIAL = re.compile(r'[^a-zA-Z0-9\s]+')

#: Person-keyed note topics whose scope is the *person* (cross-room), not
#: the room. Matches the convention ``NoteStore.recall_contact_notes``
#: builds (``contact:<participant_id>``).
CONTACT_TOPIC_PREFIX = "contact:"


def _notes_session_clause(
    sessions: list[str] | None, *, column: str,
) -> tuple[str, list[str]]:
    """Session predicate for **notes** recall (F-7 / Option A).

    Like :func:`session_in_clause`, but person-keyed ``contact:*`` notes
    bypass the session filter — their scope is the person, so they recall
    cross-room — while every other note keeps the room scoping. This is
    the single source of truth both recall paths obey (the auto-injection
    query tier and the LLM-facing ``recall_notes`` tool), so an explicit
    recall can never again be narrower than ambient injection.

    ``principal_id`` / ``epoch_id`` are applied by separate clauses and
    still strictly bound, so the widening is cross-*room* only — never
    cross-tenant or cross-epoch. In ``"*"`` no-filter mode there is no
    session predicate to widen.
    """
    sess_clause, sess_params = session_in_clause(sessions, column=column)
    if not sess_clause:
        return sess_clause, sess_params
    topic_col = column.replace("session_id", "topic")
    inner = sess_clause.lstrip()
    if inner.startswith("AND "):
        inner = inner[len("AND "):]
    # The contact-prefix LIKE value is a parameter (not interpolated); its
    # placeholder leads, so it prepends to ``sess_params`` in clause order.
    return (
        f" AND ({topic_col} LIKE ? OR {inner})",
        [f"{CONTACT_TOPIC_PREFIX}%", *sess_params],
    )


async def _recall_notes_fts5(
    db: aiosqlite.Connection,
    *,
    agent_id: str,
    query: str,
    limit: int,
    min_score: float | None,
    sessions: list[str] | None,
    note_cols: tuple[str, ...],
    principal_id: str = DEFAULT_PRINCIPAL_ID,
    epoch_id: str = DEFAULT_EPOCH_ID,
) -> list[aiosqlite.Row]:
    """FTS5 search across topic, content, and tags.

    ``sessions`` is the resolved session list from
    :func:`agents.memory._session_filter._resolve_session_list`;
    ``None`` is the ``"*"`` no-filter mode.  ``principal_id``
    (ISSUE-0081 PR 3) / ``epoch_id`` (ISSUE-0085 PR 3) are the resolved
    active tenant + run/test epoch — each unconditional strict equality,
    no carve-out.  Falls back to LIKE on FTS5 parse failure or empty
    sanitized query.
    """
    sess_clause, sess_params = _notes_session_clause(
        sessions, column="n.session_id",
    )
    princ_clause, princ_params = principal_eq_clause(
        principal_id, column="n.principal_id",
    )
    epoch_clause, epoch_params = epoch_eq_clause(
        epoch_id, column="n.epoch_id",
    )
    safe_query = _FTS5_SPECIAL.sub(" ", query).strip()
    if not safe_query:
        return await _recall_notes_like(
            db, agent_id=agent_id, query=query, limit=limit,
            min_score=min_score, sessions=sessions, note_cols=note_cols,
            principal_id=principal_id, epoch_id=epoch_id,
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
              {princ_clause}
              {epoch_clause}
            ORDER BY fts.rank * -1 DESC
            LIMIT ?
            """,
            (
                safe_query, agent_id, effective_min_score,
                *sess_params, *princ_params, *epoch_params, limit,
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
            principal_id=principal_id, epoch_id=epoch_id,
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
    principal_id: str = DEFAULT_PRINCIPAL_ID,
    epoch_id: str = DEFAULT_EPOCH_ID,
) -> list[aiosqlite.Row]:
    """LIKE fallback when FTS5 is unavailable.

    ``min_score`` is accepted for signature compatibility but not
    applied: LIKE matching is binary so every match scores ``1.0`` per
    RFC 0017 Section C.  ``principal_id`` / ``epoch_id`` — see
    :func:`_recall_notes_fts5`.
    """
    sess_clause, sess_params = _notes_session_clause(
        sessions, column="session_id",
    )
    princ_clause, princ_params = principal_eq_clause(
        principal_id, column="principal_id",
    )
    epoch_clause, epoch_params = epoch_eq_clause(
        epoch_id, column="epoch_id",
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
          {princ_clause}
          {epoch_clause}
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        (
            agent_id, pattern, pattern, pattern,
            *sess_params, *princ_params, *epoch_params, limit,
        ),
    ) as cursor:
        return list(await cursor.fetchall())


async def _recall_contact_notes(
    db: aiosqlite.Connection,
    *,
    agent_id: str,
    topic: str,
    limit: int,
    note_cols: tuple[str, ...],
    principal_id: str = DEFAULT_PRINCIPAL_ID,
    epoch_id: str = DEFAULT_EPOCH_ID,
) -> list[aiosqlite.Row]:
    """Exact-topic, **cross-session** recall (RFC 0031 §D person-keyed
    amendment, F-3b).

    Deliberately omits the ``session_in_clause`` the other note-recall
    helpers apply: a person-keyed ``contact:<id>`` note is recalled from
    *every* room, since identity attaches to the person, not the venue
    (``docs/memory-scope-axes.md``). ``principal_id`` / ``epoch_id`` are
    still enforced — cross-*room*, never cross-tenant or cross-epoch — and
    ``topic`` is matched exactly (no FTS/LIKE) so only that one person's
    contact notes cross the room boundary, never arbitrary room notes.
    """
    princ_clause, princ_params = principal_eq_clause(
        principal_id, column="principal_id",
    )
    epoch_clause, epoch_params = epoch_eq_clause(
        epoch_id, column="epoch_id",
    )
    note_select = ", ".join(note_cols)
    async with db.execute(
        f"""
        SELECT {note_select}
        FROM notes
        WHERE agent_id = ?
          AND topic = ?
          {princ_clause}
          {epoch_clause}
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        (agent_id, topic, *princ_params, *epoch_params, limit),
    ) as cursor:
        return list(await cursor.fetchall())


async def _recall_notes_recency(
    db: aiosqlite.Connection,
    *,
    agent_id: str,
    limit: int,
    sessions: list[str] | None,
    note_cols: tuple[str, ...],
    principal_id: str = DEFAULT_PRINCIPAL_ID,
    epoch_id: str = DEFAULT_EPOCH_ID,
) -> list[aiosqlite.Row]:
    """No query — return most recently updated notes.

    ``principal_id`` / ``epoch_id`` — see :func:`_recall_notes_fts5`.
    """
    sess_clause, sess_params = _notes_session_clause(
        sessions, column="session_id",
    )
    princ_clause, princ_params = principal_eq_clause(
        principal_id, column="principal_id",
    )
    epoch_clause, epoch_params = epoch_eq_clause(
        epoch_id, column="epoch_id",
    )
    note_select = ", ".join(note_cols)
    async with db.execute(
        f"""
        SELECT {note_select}
        FROM notes
        WHERE agent_id = ?
          {sess_clause}
          {princ_clause}
          {epoch_clause}
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        (agent_id, *sess_params, *princ_params, *epoch_params, limit),
    ) as cursor:
        return list(await cursor.fetchall())
