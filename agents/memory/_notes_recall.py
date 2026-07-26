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
from typing import TYPE_CHECKING

import aiosqlite

from ..epoch_id import DEFAULT_EPOCH_ID
from ..principal_id import DEFAULT_PRINCIPAL_ID
from ._epoch_filter import epoch_eq_clause
from ._principal_filter import principal_eq_clause
from ._session_filter import session_in_clause
from .episodic_queries import resolve_min_score

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

# FTS5 MATCH operator characters that cause parse errors when present in
# freeform queries — strip all non-alphanumeric characters except spaces.
_FTS5_SPECIAL = re.compile(r'[^a-zA-Z0-9\s]+')


def _protection_in_clause(
    levels: Sequence[str] | None, *, column: str,
) -> tuple[str, tuple[str, ...]]:
    """RFC 0037 §D (PR 4) — the gated-``recall_notes`` protection predicate.

    ``levels`` is the pre-resolved injectable-level IN-list from
    ``persona_runtime.classification.injectable_levels`` (this layer must
    not import the lattice; it receives the resolved set as data).  A row
    whose stored ``protection_level`` is outside the set — including a
    corrupted label — falls out of the predicate: §A rule (c)'s withhold
    realised in SQL, deliberately silent per row (the log-flood
    rationale; the Python-side gate owns the aggregated WARNING).
    ``None`` → no clause (the ungated non-persona surface).  Same
    ``(clause, params)`` shape as :func:`session_in_clause`.
    """
    if levels is None:
        return "", ()
    placeholders = ",".join("?" for _ in levels)
    return f" AND {column} IN ({placeholders})", tuple(levels)


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
    allowed_protection_levels: Sequence[str] | None = None,
) -> list[aiosqlite.Row]:
    """FTS5 search across topic, content, and tags.

    ``sessions`` is the resolved session list from
    :func:`agents.memory._session_filter._resolve_session_list`;
    ``None`` is the ``"*"`` no-filter mode.  ``principal_id``
    (ISSUE-0081 PR 3) / ``epoch_id`` (ISSUE-0085 PR 3) are the resolved
    active tenant + run/test epoch — each unconditional strict equality,
    no carve-out.  ``allowed_protection_levels`` — see
    :func:`_protection_in_clause`.  Falls back to LIKE on FTS5 parse
    failure or empty sanitized query.
    """
    sess_clause, sess_params = session_in_clause(
        sessions, column="n.session_id",
    )
    princ_clause, princ_params = principal_eq_clause(
        principal_id, column="n.principal_id",
    )
    epoch_clause, epoch_params = epoch_eq_clause(
        epoch_id, column="n.epoch_id",
    )
    prot_clause, prot_params = _protection_in_clause(
        allowed_protection_levels, column="n.protection_level",
    )
    safe_query = _FTS5_SPECIAL.sub(" ", query).strip()
    if not safe_query:
        return await _recall_notes_like(
            db, agent_id=agent_id, query=query, limit=limit,
            min_score=min_score, sessions=sessions, note_cols=note_cols,
            principal_id=principal_id, epoch_id=epoch_id,
            allowed_protection_levels=allowed_protection_levels,
        )
    effective_min_score = resolve_min_score(min_score)
    try:
        # Deterministic tiebreak (issue #740; follows #745). BM25 `fts.rank` can
        # tie — notes with identical indexed content (topic/content/tags) score
        # equally for a query — and with `LIMIT` a tie at the cutoff changes
        # *which* notes surface, not just their order, a non-portable RFC 0044
        # gap on this query-driven recall. `n.rowid` (insertion order) is the
        # tiebreak: `notes.id` is a random uuid4, so NOT portable; the query
        # already JOINs `n.rowid = fts.rowid`, and `notes` (external-content
        # FTS5) always carries a stable rowid identical across record and replay.
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
              {prot_clause}
            ORDER BY fts.rank * -1 DESC, n.rowid DESC
            LIMIT ?
            """,
            (
                safe_query, agent_id, effective_min_score,
                *sess_params, *princ_params, *epoch_params, *prot_params,
                limit,
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
            allowed_protection_levels=allowed_protection_levels,
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
    allowed_protection_levels: Sequence[str] | None = None,
) -> list[aiosqlite.Row]:
    """LIKE fallback when FTS5 is unavailable.

    ``min_score`` is accepted for signature compatibility but not
    applied: LIKE matching is binary so every match scores ``1.0`` per
    RFC 0017 Section C.  ``principal_id`` / ``epoch_id`` /
    ``allowed_protection_levels`` — see :func:`_recall_notes_fts5`.
    """
    sess_clause, sess_params = session_in_clause(
        sessions, column="session_id",
    )
    princ_clause, princ_params = principal_eq_clause(
        principal_id, column="principal_id",
    )
    epoch_clause, epoch_params = epoch_eq_clause(
        epoch_id, column="epoch_id",
    )
    prot_clause, prot_params = _protection_in_clause(
        allowed_protection_levels, column="protection_level",
    )
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped}%"
    note_select = ", ".join(note_cols)
    # Deterministic tiebreak (issue #740; follows #739 / #742–#744). `updated_at`
    # can tie — notes written in one instant under the eval driver's FrozenClock —
    # and with `LIMIT` a tie at the cutoff changes *which* rows are returned, not
    # just their order, a non-portable RFC 0044 golden-trace gap. `rowid`
    # (insertion order) is the tiebreak: `notes.id` is a random uuid4, so it is
    # NOT portable; `notes` is an external-content FTS5 source
    # (`content_rowid=rowid`), so it always carries a stable rowid (never WITHOUT
    # ROWID) that is identical across record and replay. Plain row SELECT.
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
          {prot_clause}
        ORDER BY updated_at DESC, rowid DESC
        LIMIT ?
        """,
        (
            agent_id, pattern, pattern, pattern,
            *sess_params, *princ_params, *epoch_params, *prot_params, limit,
        ),
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
    allowed_protection_levels: Sequence[str] | None = None,
) -> list[aiosqlite.Row]:
    """No query — return most recently updated notes.

    ``principal_id`` / ``epoch_id`` / ``allowed_protection_levels`` —
    see :func:`_recall_notes_fts5`.
    """
    sess_clause, sess_params = session_in_clause(
        sessions, column="session_id",
    )
    princ_clause, princ_params = principal_eq_clause(
        principal_id, column="principal_id",
    )
    epoch_clause, epoch_params = epoch_eq_clause(
        epoch_id, column="epoch_id",
    )
    prot_clause, prot_params = _protection_in_clause(
        allowed_protection_levels, column="protection_level",
    )
    note_select = ", ".join(note_cols)
    # Deterministic `rowid` tiebreak — same rationale as `_recall_notes_like`
    # above (issue #740): `notes.id` is a uuid4, `notes` carries a stable FTS
    # rowid, and the `LIMIT` makes equal-`updated_at` ties consequential.
    async with db.execute(
        f"""
        SELECT {note_select}
        FROM notes
        WHERE agent_id = ?
          {sess_clause}
          {princ_clause}
          {epoch_clause}
          {prot_clause}
        ORDER BY updated_at DESC, rowid DESC
        LIMIT ?
        """,
        (
            agent_id, *sess_params, *princ_params, *epoch_params,
            *prot_params, limit,
        ),
    ) as cursor:
        return list(await cursor.fetchall())
