"""
Read-only query helpers and shared validators for RelationshipMemory.

All functions accept an open ``aiosqlite.Connection``, an ``agent_id``
string, and optional type parameters. They carry no object state and are
safe to call from any async context.
"""

from __future__ import annotations

import json
import logging
from typing import Any, cast

import aiosqlite
from opentelemetry import trace

from ..epoch_id import DEFAULT_EPOCH_ID
from ..observability.spans import RELATIONSHIP_LOOKUP_SPAN
from ..principal_id import DEFAULT_PRINCIPAL_ID
from ._epoch_filter import epoch_eq_clause
from ._principal_filter import principal_eq_clause
from ._session_filter import session_in_clause
from .relationship_types import (
    _DEFAULT_TRUST,
    _MAX_RECENT_INTERACTIONS,
    Interaction,
    RelationshipSummary,
)

logger = logging.getLogger(__name__)
_tracer = trace.get_tracer(__name__)

__all__ = [
    "get_all_relationships",
    "get_identity",
    "get_relationship_summary",
    "get_trust",
    "validate_other_id",
    "validate_participant_types",
    "truncate_field",
]


# ─── Shared validators & helpers ────────────────────────────────────────────


def validate_other_id(other_id: str) -> None:
    """Reject empty other_id (F-4-1)."""
    if not other_id or not other_id.strip():
        raise ValueError("other_id must not be empty")


def validate_participant_types(
    participant_type: str, other_participant_type: str,
) -> None:
    """Validate participant types at write boundary (OQ 3)."""
    from ..participant import validate_participant_type
    validate_participant_type(participant_type)
    validate_participant_type(other_participant_type)


def truncate_field(
    agent_id: str, value: str, other_id: str, label: str,
) -> str:
    """Cap a string field to 1024 chars to prevent unbounded storage."""
    if len(value) > 1024:
        logger.warning(
            "%s truncated from %d to 1024 chars for %s→%s",
            label, len(value), agent_id, other_id,
        )
        return value[:1021] + "..."
    return value


# ─── Read queries ────────────────────────────────────────────────────────────


async def get_trust(
    db: aiosqlite.Connection,
    agent_id: str,
    other_id: str,
    *,
    participant_type: str = "agent",
    other_participant_type: str = "agent",
    sessions: list[str] | None = None,
    principal_id: str = DEFAULT_PRINCIPAL_ID,
    epoch_id: str = DEFAULT_EPOCH_ID,
) -> float:
    """Get current trust score for another participant (0.0–1.0).

    Returns the default (0.5) if no relationship exists.

    ``sessions`` (RFC 0031 Phase 2 PR 3) is a resolved list from
    :func:`agents.memory._session_filter._resolve_session_list` —
    ``None`` is the ``"*"`` no-filter mode.  ``principal_id``
    (ISSUE-0081 PR 3) / ``epoch_id`` (ISSUE-0085 PR 3) are the resolved
    active tenant + run/test epoch — each unconditional strict equality,
    no carve-out — so a foreign-tenant or prior-run trust value cannot
    leak into the prompt.
    """
    sess_clause, sess_params = session_in_clause(sessions, column="session_id")
    princ_clause, princ_params = principal_eq_clause(
        principal_id, column="principal_id",
    )
    epoch_clause, epoch_params = epoch_eq_clause(
        epoch_id, column="epoch_id",
    )
    attrs = {"agent.id": agent_id, "participant.id": other_id}
    with _tracer.start_as_current_span(RELATIONSHIP_LOOKUP_SPAN, attributes=attrs):
        async with db.execute(
            "SELECT trust_score FROM relationships "
            "WHERE participant_id = ? AND participant_type = ? "
            "AND other_participant_id = ? AND other_participant_type = ?"
            f"{sess_clause}{princ_clause}{epoch_clause}",
            (agent_id, participant_type, other_id, other_participant_type,
             *sess_params, *princ_params, *epoch_params),
        ) as cursor:
            row = await cursor.fetchone()
        return row[0] if row is not None else _DEFAULT_TRUST


async def get_identity(
    db: aiosqlite.Connection,
    agent_id: str,
    other_id: str,
    *,
    participant_type: str = "agent",
    other_participant_type: str = "agent",
    principal_id: str = DEFAULT_PRINCIPAL_ID,
    epoch_id: str = DEFAULT_EPOCH_ID,
) -> dict[str, Any] | None:
    """Read the structured person identity off the relationship record.

    RFC 0031 amendment (F-7 Option D, ISSUE-0093) — the cross-room read for
    person identity (name / role / stable preferences).

    **No session filter, by design.**  Unlike :func:`get_trust` /
    :func:`get_relationship_summary` (which apply the §D
    :func:`session_in_clause` to the relationship row), identity recall
    omits the session predicate entirely.  The relationship primary key
    excludes ``session_id`` — there is exactly one row per ``(participant
    tuple, principal, epoch)`` — so leaving the session axis out of the
    query is precisely what makes identity *cross-room by construction*:
    identity stated in room A surfaces in room B.  This is strictly narrower
    than the Option-A ``contact:*`` carve-out (no ``sessions="*"`` sentinel
    anywhere) — the room axis simply is not part of the tier's key.

    ``principal_id`` / ``epoch_id`` remain strict equality (each part of the
    PK), so cross-room is never cross-tenant or cross-epoch.

    Returns the decoded identity object, or ``None`` if the row is absent or
    has no identity recorded (a row created via trust / interaction only).
    """
    princ_clause, princ_params = principal_eq_clause(
        principal_id, column="principal_id",
    )
    epoch_clause, epoch_params = epoch_eq_clause(
        epoch_id, column="epoch_id",
    )
    attrs = {"agent.id": agent_id, "participant.id": other_id}
    with _tracer.start_as_current_span(RELATIONSHIP_LOOKUP_SPAN, attributes=attrs):
        async with db.execute(
            "SELECT identity FROM relationships "
            "WHERE participant_id = ? AND participant_type = ? "
            "AND other_participant_id = ? AND other_participant_type = ?"
            f"{princ_clause}{epoch_clause}",
            (agent_id, participant_type, other_id, other_participant_type,
             *princ_params, *epoch_params),
        ) as cursor:
            row = await cursor.fetchone()
    if row is None or row[0] is None:
        return None
    return cast("dict[str, Any]", json.loads(row[0]))


async def get_relationship_summary(
    db: aiosqlite.Connection,
    agent_id: str,
    other_id: str,
    *,
    participant_type: str = "agent",
    other_participant_type: str = "agent",
    sessions: list[str] | None = None,
    principal_id: str = DEFAULT_PRINCIPAL_ID,
    epoch_id: str = DEFAULT_EPOCH_ID,
) -> RelationshipSummary:
    """Get full relationship context for injection into LLM prompt.

    ``sessions`` — see :func:`get_trust`.  The §D filter applies to
    every fetch in this function:

    * the ``relationships`` row (PR 3 — visibility of the row itself),
    * the ``interactions`` recent-history page (PR 5, migration v10 —
      `ISSUE-0080
      <../../docs/issues/ISSUE-0080-relationship-recent-interactions-cross-session-leak.md>`_),
    * the ``MIN(created_at)`` / ``MAX(created_at)`` first/last-
      interaction-at span lookup (PR 5, same).

    ``interaction_count`` on the returned summary is the **per-session
    count derived from the filtered ``interactions`` subquery** (Policy
    (C) from `ISSUE-0080 §4`).  The ``relationships.interaction_count``
    column survives unchanged for the unfiltered admin / debug path,
    but the prompt-injection surface returns a count that matches what
    the persona can actually see in ``recent_interactions``.

    ``last_interaction_at`` is likewise derived from the filtered
    interactions subquery (``MAX(created_at)``), **not** read from the
    ``relationships`` column: ``record_interaction``'s ``ON CONFLICT``
    refreshes that column keyed on the participant 4-tuple with no
    session predicate, so a cross-session write bumps the first-seen
    row's timestamp.  Reading the column would surface another session's
    "Last seen" (and skew the RFC 0021 cadence upper bound) — the same
    leak class as ``recent_interactions`` / ``first_interaction_at``.
    """
    rel_sess_clause, rel_sess_params = session_in_clause(
        sessions, column="session_id",
    )
    # ISSUE-0081 PR 3 — strict tenant equality on every fetch in this
    # function (``relationships`` row + both ``interactions`` subqueries).
    # The ``relationships`` and ``interactions`` SELECTs here are
    # single-table (unaliased), so one ``principal_id`` clause is reused.
    princ_clause, princ_params = principal_eq_clause(
        principal_id, column="principal_id",
    )
    # ISSUE-0085 PR 3 — strict epoch equality on every fetch in this
    # function, reused like ``princ_clause`` (all SELECTs here are
    # single-table / unaliased, so one ``epoch_id`` clause suffices).
    epoch_clause, epoch_params = epoch_eq_clause(
        epoch_id, column="epoch_id",
    )
    # Fetch relationship row.  ``interaction_count`` and
    # ``last_interaction_at`` from this row are not surfaced to the
    # prompt; we derive per-session values below from the filtered
    # ``interactions`` subquery instead (PR 5 ISSUE-0080 Policy C — the
    # columns survive unchanged for the unfiltered admin / debug path).
    async with db.execute(
        "SELECT trust_score, notes "
        "FROM relationships "
        "WHERE participant_id = ? AND participant_type = ? "
        "AND other_participant_id = ? AND other_participant_type = ?"
        f"{rel_sess_clause}{princ_clause}{epoch_clause}",
        (agent_id, participant_type, other_id, other_participant_type,
         *rel_sess_params, *princ_params, *epoch_params),
    ) as cursor:
        row = await cursor.fetchone()

    if row is None:
        return RelationshipSummary(
            other_participant_id=other_id,
            other_participant_type=other_participant_type,
            trust_score=_DEFAULT_TRUST,
            interaction_count=0,
            last_interaction_at=None,
            notes=None,
        )

    trust_score, notes = row

    # Both ``interactions`` SELECTs below carry the §D filter — PR 5 /
    # ISSUE-0080 fix.  ``interactions`` rows now carry ``session_id``
    # (migration v10) and ``record_interaction`` threads the active
    # session id onto every INSERT.
    int_sess_clause, int_sess_params = session_in_clause(
        sessions, column="session_id",
    )

    # Fetch recent interactions (session- + principal-filtered).
    async with db.execute(
        "SELECT id, participant_id, participant_type, "
        "other_participant_id, other_participant_type, "
        "interaction_type, outcome, sentiment, created_at "
        "FROM interactions "
        "WHERE participant_id = ? AND participant_type = ? "
        "AND other_participant_id = ? AND other_participant_type = ? "
        f"{int_sess_clause}{princ_clause}{epoch_clause} "
        "ORDER BY created_at DESC LIMIT ?",
        (agent_id, participant_type, other_id,
         other_participant_type, *int_sess_params, *princ_params,
         *epoch_params, _MAX_RECENT_INTERACTIONS),
    ) as cursor:
        interaction_rows = await cursor.fetchall()

    recent = [
        Interaction(
            id=r[0],
            participant_id=r[1],
            participant_type=r[2],
            other_participant_id=r[3],
            other_participant_type=r[4],
            interaction_type=r[5],
            outcome=r[6],
            sentiment=r[7],
            created_at=r[8],
        )
        for r in interaction_rows
    ]

    # Per-session interaction count derived from the filtered subquery
    # — PR 5 / ISSUE-0080 Policy (C).  Matches what ``recent_interactions``
    # surfaces to the LLM.
    async with db.execute(
        "SELECT COUNT(*) FROM interactions "
        "WHERE participant_id = ? AND participant_type = ? "
        "AND other_participant_id = ? AND other_participant_type = ?"
        f"{int_sess_clause}{princ_clause}{epoch_clause}",
        (agent_id, participant_type, other_id, other_participant_type,
         *int_sess_params, *princ_params, *epoch_params),
    ) as cursor:
        count_row = await cursor.fetchone()
    interaction_count = int(count_row[0]) if count_row is not None else 0

    # RFC 0021 PR 2: cadence rendering needs the relationship's first +
    # last interaction timestamps.  PR 5 / ISSUE-0080: both bounds are
    # derived from the session-filtered subquery so they reflect the
    # active session's span, not the global one — ``last_interaction_at``
    # in particular must not inherit the cross-session ON-CONFLICT bump
    # on the ``relationships`` column.  ``MIN``/``MAX`` over an empty
    # filtered set return ``(NULL, NULL)``, collapsing both to ``None``.
    async with db.execute(
        "SELECT MIN(created_at), MAX(created_at) FROM interactions "
        "WHERE participant_id = ? AND participant_type = ? "
        "AND other_participant_id = ? AND other_participant_type = ?"
        f"{int_sess_clause}{princ_clause}{epoch_clause}",
        (agent_id, participant_type, other_id, other_participant_type,
         *int_sess_params, *princ_params, *epoch_params),
    ) as cursor:
        span_row = await cursor.fetchone()
    first_interaction_at = span_row[0] if span_row is not None else None
    last_interaction_at = span_row[1] if span_row is not None else None

    return RelationshipSummary(
        other_participant_id=other_id,
        other_participant_type=other_participant_type,
        trust_score=trust_score,
        interaction_count=interaction_count,
        last_interaction_at=last_interaction_at,
        notes=notes,
        recent_interactions=recent,
        first_interaction_at=first_interaction_at,
    )


async def get_all_relationships(
    db: aiosqlite.Connection,
    agent_id: str,
    *,
    participant_type: str = "agent",
    sessions: list[str] | None = None,
    principal_id: str = DEFAULT_PRINCIPAL_ID,
    epoch_id: str = DEFAULT_EPOCH_ID,
) -> list[RelationshipSummary]:
    """Get summaries for all known relationships of an agent.

    .. note::

       ``recent_interactions`` is not populated in returned summaries
       (defaults to ``[]``) to avoid N+1 queries. Use
       ``get_relationship_summary()`` for individual relationships
       with full interaction history.

    ``sessions`` — see :func:`get_trust`.

    ``interaction_count`` and ``last_interaction_at`` are derived
    per-session from the filtered ``interactions`` subquery — Policy (C)
    from `ISSUE-0080
    <../../docs/issues/ISSUE-0080-relationship-recent-interactions-cross-session-leak.md>`_,
    applied uniformly to both list- and summary-mode reads so cadence
    aggregations do not inherit the cross-session-inflated count or the
    cross-session ``last_interaction_at`` bump on the ``relationships``
    column.
    """
    rel_sess_clause, rel_sess_params = session_in_clause(
        sessions, column="r.session_id",
    )
    int_sess_clause, int_sess_params = session_in_clause(
        sessions, column="i.session_id",
    )
    # ISSUE-0081 PR 3 — strict tenant equality on both JOIN sides.
    rel_princ_clause, rel_princ_params = principal_eq_clause(
        principal_id, column="r.principal_id",
    )
    int_princ_clause, int_princ_params = principal_eq_clause(
        principal_id, column="i.principal_id",
    )
    # ISSUE-0085 PR 3 — strict epoch equality on both JOIN sides.
    rel_epoch_clause, rel_epoch_params = epoch_eq_clause(
        epoch_id, column="r.epoch_id",
    )
    int_epoch_clause, int_epoch_params = epoch_eq_clause(
        epoch_id, column="i.epoch_id",
    )
    # LEFT JOIN aggregates the per-session count + last-interaction
    # timestamp from ``interactions`` onto each visible relationship row.
    # Same predicate shape on both sides; ``COUNT(i.id)`` yields 0 and
    # ``MAX(i.created_at)`` yields NULL for relationships with no
    # in-session interactions (a row tagged ``legacy`` that has only
    # been seeded via config, for instance).  ``MAX(i.created_at)``
    # replaces ``r.last_interaction_at`` so list-mode reads do not
    # surface the cross-session ON-CONFLICT bump on the column (PR 5 /
    # ISSUE-0080).
    async with db.execute(
        "SELECT r.other_participant_id, r.other_participant_type, "
        "r.trust_score, COUNT(i.id) AS in_session_count, "
        "MAX(i.created_at) AS in_session_last, r.notes "
        "FROM relationships r "
        "LEFT JOIN interactions i ON "
        "  i.participant_id = r.participant_id "
        "  AND i.participant_type = r.participant_type "
        "  AND i.other_participant_id = r.other_participant_id "
        "  AND i.other_participant_type = r.other_participant_type"
        f"{int_sess_clause}{int_princ_clause}{int_epoch_clause} "
        "WHERE r.participant_id = ? AND r.participant_type = ?"
        f"{rel_sess_clause}{rel_princ_clause}{rel_epoch_clause} "
        "GROUP BY r.participant_id, r.participant_type, "
        "  r.other_participant_id, r.other_participant_type, "
        "  r.trust_score, r.notes "
        "ORDER BY r.trust_score DESC",
        (
            *int_sess_params, *int_princ_params, *int_epoch_params,
            agent_id, participant_type,
            *rel_sess_params, *rel_princ_params, *rel_epoch_params,
        ),
    ) as cursor:
        rows = await cursor.fetchall()

    return [
        RelationshipSummary(
            other_participant_id=r[0],
            other_participant_type=r[1],
            trust_score=r[2],
            interaction_count=int(r[3]),
            last_interaction_at=r[4],
            notes=r[5],
        )
        for r in rows
    ]
