"""
Procedural-tier read/write helpers for ``EpisodicMemory`` (RFC 0008 PR 5).

Extracted from :mod:`agents.memory.episodic` so the SQLite-write surface
in that module stays under the 500-line repo soft cap (see
:mod:`scripts.checks.file_size`).  The procedural tier reuses the
``episodes`` table — procedural rows are episodic rows that carry a
``procedure:KEY`` tag in ``tags_json`` and populate the ``confidence`` /
``last_validated_at`` columns added by migration v6.

The helpers are plain coroutines that take the connection, so
:class:`EpisodicMemory` carries no procedural methods.  The public
surface is :meth:`MemoryStore.store_procedure` /
:meth:`MemoryStore.retrieve_procedures`
(:class:`~agents.memory.facade_procedural.ProceduralFacadeMixin`), which
calls these helpers with the facade's connection.
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass
from typing import Any

import aiosqlite

from ..epoch_id import DEFAULT_EPOCH_ID
from ..principal_id import DEFAULT_PRINCIPAL_ID
from ._epoch_filter import epoch_eq_clause as _epoch_eq_clause
from ._principal_filter import principal_eq_clause as _principal_eq_clause
from ._session_filter import session_in_clause as _session_in_clause
from .decay import (
    DEFAULT_C_MIN,
    DEFAULT_LAMBDA_PER_DAY,
    compute_decayed_confidence,
)
from .episodic_queries import MAX_RECALL_LIMIT

# PR 6b review Should-Fix #1: defence-in-depth log-safety on the
# ``stale_memory_injection`` warn-log ``key`` field.  Procedural rows
# written *before* PR 6b's facade validator landed could carry
# arbitrary characters (the prior validator only rejected empty /
# whitespace keys), so the value pulled out of ``tags_json`` is not
# guaranteed to be control-char-free even though every *new* row is.
# ``_log_safety`` is dependency-free and explicitly anticipates this
# callsite in its module docstring.
#
# The import is *lazy* (inside :func:`_admit`) because
# importing it at module load-time would trigger
# ``agents/sub_agents/__init__.py`` → spawner → ``agents.base`` →
# ``agents.memory`` → this module — a circular import while
# ``agents.base`` is still being initialised.  Importing inside the
# warn-log path side-steps the cycle and keeps the runtime cost out
# of the hot path (the import is cached after first use).

logger = logging.getLogger(__name__)

__all__ = [
    "ProcedureRecallEntry",
    "extract_procedure_key",
    "recall_procedures",
    "refresh_confidence",
    "resolve_base_confidence",
]


# PR #225 review S1: LIKE wildcards (``%`` ``_``) inside caller-supplied
# ``key`` / ``query`` would otherwise widen the match.  SQL injection is
# already closed via ``?``-binding, but a key like ``"50% off"`` (LLM
# generated) would otherwise mass-refresh / mass-recall sibling
# procedures.  We backslash-escape the three LIKE meta-characters and
# pair every affected LIKE clause with ``ESCAPE '\\'``.
_LIKE_META_CHARS = ("\\", "%", "_")


def _escape_like(s: str) -> str:
    """Backslash-escape SQLite LIKE meta-characters in *s*.

    Caller is responsible for pairing the resulting pattern with an
    ``ESCAPE '\\'`` clause in the SQL — without that clause SQLite
    treats backslash as a literal character (no escape semantics).
    The backslash itself is escaped first so the subsequent ``%`` /
    ``_`` substitutions do not double-escape it.
    """
    for ch in _LIKE_META_CHARS:
        s = s.replace(ch, "\\" + ch)
    return s


@dataclass(frozen=True)
class ProcedureRecallEntry:
    """A single procedural row returned by :func:`recall_procedures`.

    Distinct from :class:`~agents.memory.episodic_queries.Episode`
    because the procedural read path selects only the columns the decay
    computation needs and projects the ``procedure:{key}`` tag back into
    a structured ``key`` field so callers do not have to re-parse the
    tag list.

    ``base_confidence`` carries the stored ``c_0`` value (after the
    legacy-row compatibility shim, :func:`resolve_base_confidence`);
    operators can subtract it from ``decayed_confidence`` to get the
    per-row decay delta when correlating the ``stale_memory_injection``
    log with a specific entry.
    """

    id: str
    key: str | None
    content: str
    decayed_confidence: float
    base_confidence: float
    last_validated_at: float | None
    created_at: float


def extract_procedure_key(tags: list[str]) -> str | None:
    """Return the first ``procedure:KEY`` tag's KEY suffix, or ``None``.

    Procedural rows are always written with a single ``procedure:KEY``
    tag by :meth:`MemoryStore.store_procedure`, but the helper is
    defensive about extra tags so a future change adding decoration
    tags (e.g. ``"category:tools"``) does not break recall.
    """
    for tag in tags:
        if isinstance(tag, str) and tag.startswith("procedure:"):
            return tag[len("procedure:"):]
    return None


async def recall_procedures(
    db: aiosqlite.Connection,
    agent_id: str,
    *,
    query: str = "",
    limit: int = 10,
    c_min: float = DEFAULT_C_MIN,
    lambda_per_day: float = DEFAULT_LAMBDA_PER_DAY,
    stale_threshold: float | None = None,
    now: float | None = None,
    session_list: list[str] | None = None,
    principal_id: str = DEFAULT_PRINCIPAL_ID,
    epoch_id: str = DEFAULT_EPOCH_ID,
) -> list[ProcedureRecallEntry]:
    """Return procedural entries with read-time confidence decay applied.

    The facade entry point is
    :meth:`~agents.memory.facade_procedural.ProceduralFacadeMixin.retrieve_procedures`,
    which documents ``query``, ``limit``, ``now`` and ``sessions``, takes the
    decay knobs from the agent's config and the tenant and epoch from the
    active scope; this helper holds the SQL.

    Procedural rows are identified by the ``procedure:`` tag prefix
    written by :meth:`MemoryStore.store_procedure`.  Each row's decayed
    confidence is computed via
    :func:`~agents.memory.decay.compute_decayed_confidence` against the
    row's ``last_validated_at`` (or ``created_at`` when never
    validated).  Rows whose decayed value is below ``c_min`` are
    filtered out before the ``limit`` slice.

    ``limit`` below 1 raises :class:`ValueError`, and a larger one is
    capped at ``MAX_RECALL_LIMIT`` (100), as the episodic and notes reads do.

    PR 6b (PR 5 R1 S3 + Info-3): the SQL WHERE pre-filters rows whose
    anchor is older than ``t_max = -ln(c_min) / lambda_per_day`` days, the
    age at which even a base of 1.0 has decayed below ``c_min``, so the
    decay loop does not walk rows that cannot pass.  When
    ``lambda_per_day == 0`` (decay disabled) ``t_max`` is infinite and the
    cutoff is omitted.  A row younger than ``t_max`` can still fail, because
    its base may be below 1.0, so one statement reads the rows newest first
    and the loop stops once ``limit`` of them pass.

    PR 6b (PR 5 R2 Mi2): when ``stale_threshold`` is supplied, this
    helper emits a ``stale_memory_injection`` structured log for each
    admitted entry whose decayed confidence falls in
    ``[c_min, stale_threshold)``.  The log lives next to the decayed
    value that triggers it; the facade mixin no longer wraps the
    return value to do this.  ``stale_threshold=None`` (the default)
    turns the alert off; the facade always passes its configured
    threshold.
    """
    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}")
    if limit > MAX_RECALL_LIMIT:
        logger.warning(
            "limit=%d exceeds maximum (%d), capping", limit, MAX_RECALL_LIMIT,
        )
        limit = MAX_RECALL_LIMIT
    timestamp = now if now is not None else time.time()
    # Compute the SQL-side cutoff (PR 5 R1 S3).  When ``lambda_per_day``
    # is zero, the closed-form ``t_max`` is undefined (division by
    # zero) — fall back to "no SQL cutoff" so legacy decay-disabled
    # deployments behave identically.  Same for ``c_min == 0`` (then
    # ``ln(0)`` is undefined and every non-zero decay value is
    # admitted).
    sql_cutoff_seconds: float | None = None
    if lambda_per_day > 0 and c_min > 0:
        # ``compute_decayed_confidence`` interprets ``age`` as seconds
        # (with ``lambda_per_day`` divided by 86_400 internally), so the
        # cutoff in seconds is ``-ln(c_min)/lambda_per_day * 86400``.
        sql_cutoff_seconds = -math.log(c_min) / lambda_per_day * 86400.0
    # Procedural rows carry a tag formatted ``procedure:{key}`` — see
    # ``MemoryStore.store_procedure``.  ``tags_json`` stores the tag
    # list as a JSON array, so the LIKE pattern matches the tag
    # substring verbatim regardless of array position.  Phase 5 uses
    # ``LIKE`` rather than FTS5 so a procedure key with punctuation
    # can be matched verbatim by the caller without FTS5-sanitising.
    # NB: every appended fragment below owns its own leading space,
    # and ``sql_base`` does not carry one at its end — the resulting
    # composed SQL contains no doubled whitespace.  (The single space
    # inside ``"agent_id = ? AND tags_json…"`` is internal token
    # separation, not a trailing space.)  PR #451 deep-review nit; L2
    # carry-forward clarified the wording.
    sql_base = (
        "SELECT id, summary, tags_json, confidence, "
        "last_validated_at, created_at, importance "
        "FROM episodes WHERE agent_id = ? "
        "AND tags_json LIKE '%\"procedure:%'"
    )
    params: list[Any] = [agent_id]
    # Session, tenant and epoch scope, from the helper the refresh uses too.
    scope_clause, scope_params = _scope_clause(session_list, principal_id, epoch_id)
    sql_base += scope_clause
    params.extend(scope_params)
    if sql_cutoff_seconds is not None:
        # COALESCE(last_validated_at, created_at) is the same anchor
        # the application-side decay uses, so the cutoff cannot
        # exclude a row the in-Python loop would have admitted.
        sql_base += (
            " AND (? - COALESCE(last_validated_at, created_at)) <= ?"
        )
        params.extend([timestamp, sql_cutoff_seconds])
    if query:
        # PR #225 review S1: escape LIKE meta-chars in ``query`` so a
        # ``%`` / ``_`` in caller-supplied search text does not widen
        # the match.  Pair with ``ESCAPE '\\'``.
        sql_base += " AND summary LIKE ? ESCAPE '\\'"
        params.append(f"%{_escape_like(query)}%")
    # One statement reads the rows newest first, and the loop stops once
    # ``limit`` of them pass the decay filter.  Decay is computed in Python,
    # so a SQL LIMIT would cut before the filter: newer rows that decay below
    # ``c_min`` could fill it and hide older rows that pass.  SQLite sorts
    # the matching rows before it returns the first, so a write on this
    # connection while the cursor is open does not change what it returns.
    #
    # Deterministic tiebreak (issue #740; follows #739 / #742). `created_at`
    # can tie — several procedures stored in one instant under the eval
    # driver's FrozenClock — so `rowid` (insertion order) completes the sort
    # key. Without it SQLite may order tied rows either way, which changes
    # *which* rows a recall returns, not just their order — a non-portable
    # RFC 0044 golden-trace gap. `episodes.id` is a random uuid4
    # (`insert_episode`), so it is NOT a portable tiebreak; `rowid` is
    # identical across record and replay, which INSERT the same episodes in
    # the same order, and the table is not WITHOUT ROWID. This is a plain row
    # SELECT, so `rowid` is unambiguous (one per row).
    sql = sql_base + " ORDER BY created_at DESC, rowid DESC"
    out: list[ProcedureRecallEntry] = []
    async with db.execute(sql, tuple(params)) as cursor:
        async for row in cursor:
            entry = _admit(
                row,
                agent_id=agent_id,
                timestamp=timestamp,
                lambda_per_day=lambda_per_day,
                c_min=c_min,
                stale_threshold=stale_threshold,
            )
            if entry is not None:
                out.append(entry)
                if len(out) >= limit:
                    break
    return out


def _scope_clause(
    session_list: list[str] | None,
    principal_id: str,
    epoch_id: str,
) -> tuple[str, list[Any]]:
    """Build the scope clauses a procedural recall and refresh both append.

    One builder, so the two cannot drift apart on scope.  ``session_list``
    is the resolved RFC 0031 §D list (``legacy`` carve-out included);
    ``None`` is the ``"*"`` sentinel and adds no session clause.  Tenant
    (ISSUE-0081 PR 3) and epoch (ISSUE-0085 PR 3) are strict equality with
    no ``"*"`` bypass: another tenant's or epoch's row is never read or
    refreshed, even on a ``sessions="*"`` read.
    """
    session_sql, session_params = _session_in_clause(
        session_list, column="session_id",
    )
    principal_sql, principal_params = _principal_eq_clause(
        principal_id, column="principal_id",
    )
    epoch_sql, epoch_params = _epoch_eq_clause(epoch_id, column="epoch_id")
    return (
        f"{session_sql}{principal_sql}{epoch_sql}",
        [*session_params, *principal_params, *epoch_params],
    )


def _admit(
    row: Any,
    *,
    agent_id: str,
    timestamp: float,
    lambda_per_day: float,
    c_min: float,
    stale_threshold: float | None,
) -> ProcedureRecallEntry | None:
    """Decay one :func:`recall_procedures` row and return its entry.

    Returns ``None`` when the decayed confidence is below ``c_min``, and
    logs ``stale_memory_injection`` for an entry that passes but sits
    below ``stale_threshold``.
    """
    (
        row_id, summary, tags_json, confidence, last_val, created_at,
        importance,
    ) = row
    base_conf = resolve_base_confidence(confidence, importance, last_val)
    anchor = last_val if last_val is not None else created_at
    age = timestamp - float(anchor)
    decayed = compute_decayed_confidence(
        base_conf, age, lambda_per_day=lambda_per_day,
    )
    if decayed < c_min:
        return None
    tags = json.loads(tags_json) if tags_json else []
    entry = ProcedureRecallEntry(
        id=row_id,
        key=extract_procedure_key(tags),
        content=summary,
        decayed_confidence=decayed,
        base_confidence=base_conf,
        last_validated_at=(
            float(last_val) if last_val is not None else None
        ),
        created_at=float(created_at),
    )
    if (
        stale_threshold is not None
        and decayed < stale_threshold
    ):
        # Structured log — the orchestrator-side log ingestion
        # path (RFC 0019 PR 4 LogServiceServer) increments the
        # ``orchestrator.memory.stale_memory_injection`` counter
        # when it sees this event.  Field names are part of the
        # log contract and must match the Go-side parser.
        #
        # PR 6b review Should-Fix #1: lazy-import ``_bounded`` to
        # avoid a circular import via ``agents.sub_agents`` at
        # module load time (see module-level comment above).
        from ..sub_agents._log_safety import bounded as _bounded
        logger.warning(
            "stale_memory_injection",
            extra={
                "metric": "stale_memory_injection",
                "agent_id": agent_id,
                # PR 6b review Should-Fix #1: ``entry.key`` is
                # extracted from ``tags_json`` and may carry
                # control characters on rows written before the
                # facade-side regex validator landed.  Pipe through
                # ``_bounded`` to neutralise CWE-117 log-injection
                # vectors on legacy rows; new rows pass through
                # unchanged because the facade regex already
                # rejects control chars.
                "key": _bounded(entry.key) if entry.key else None,
                "decayed_confidence": entry.decayed_confidence,
                "base_confidence": entry.base_confidence,
                "c_min": c_min,
                "stale_threshold": stale_threshold,
            },
        )
    return entry


async def refresh_confidence(
    db: aiosqlite.Connection,
    agent_id: str,
    key: str,
    *,
    session_list: list[str] | None,
    principal_id: str = DEFAULT_PRINCIPAL_ID,
    epoch_id: str = DEFAULT_EPOCH_ID,
) -> bool:
    """Mark every procedure row tagged ``procedure:{key}`` as freshly validated.

    Sets ``confidence`` to ``1.0`` and ``last_validated_at`` to
    ``time.time()`` on the matching rows for ``agent_id``.  Returns
    ``True`` when at least one row was updated.

    Implements the "Confidence refresh on successful reuse" contract
    from RFC 0008 §G — :meth:`MemoryStore.store_procedure` invokes it
    automatically when a re-store hits an existing key.  The stamp is what
    restarts decay from 1.0: :func:`resolve_base_confidence` reads
    ``importance`` for a row at confidence 1.0 only while
    ``last_validated_at`` is NULL (a legacy row).  ``importance`` itself
    is left alone, because ordinary episodic recall ranks and filters on it.

    The match uses the scope clauses :func:`recall_procedures` uses
    (:func:`_scope_clause`) and the exact key:

    * ``session_list`` is required: the resolved list, ``legacy`` carve-out
      included, or ``None`` for every session, as ``"*"`` is for recall.
      :meth:`MemoryStore.store_procedure` passes its write session plus
      ``legacy``.
    * ``principal_id`` (ISSUE-0081 PR 3; default :data:`DEFAULT_PRINCIPAL_ID`)
      and ``epoch_id`` (ISSUE-0085 PR 3) use the same strict equality.
    * The key is compared with its case, as the key validator does.

    It ignores decay, so a row that recall hides (below ``c_min``, or past
    the age cutoff) is refreshed too: that is how a reuse revives it.

    Without one of the scope clauses, a re-store under another session,
    tenant or epoch refreshed that scope's row, and because
    ``store_procedure`` skips the insert when a refresh matches, its own
    write was dropped.
    """
    if not key or not key.strip():
        raise ValueError("key must not be empty")
    # PR #225 review S1: ``key`` flows from callers (and ultimately from
    # LLM-generated procedure names).  A literal ``%`` or ``_`` would
    # otherwise widen the LIKE match and silently refresh sibling
    # procedures.  Escape the LIKE meta-chars and pair with
    # ``ESCAPE '\\'``.  The surrounding ``"…"`` quotes (always present
    # because ``json.dumps`` quotes every list element) still anchor the
    # match to a single tag-array element.
    pattern = f'%"procedure:{_escape_like(key)}"%'
    # SQLite ``LIKE`` ignores ASCII case, and keys do not, so ``instr`` also
    # checks the tag's exact JSON text: re-storing ``deploy`` must not
    # refresh ``Deploy`` and skip its own insert.
    scope_clause, scope_params = _scope_clause(session_list, principal_id, epoch_id)
    cursor = await db.execute(
        "UPDATE episodes SET confidence = 1.0, last_validated_at = ? "
        "WHERE agent_id = ? AND tags_json LIKE ? ESCAPE '\\' "
        f"AND instr(tags_json, ?) > 0{scope_clause}",
        (
            time.time(), agent_id, pattern, json.dumps(f"procedure:{key}"),
            *scope_params,
        ),
    )
    await db.commit()
    return (cursor.rowcount or 0) > 0


def resolve_base_confidence(
    confidence: float | None,
    importance: float | None,
    last_validated_at: float | None = None,
) -> float:
    """Return the ``c_0`` value to feed into the decay formula.

    Legacy PR 2 procedural rows wrote confidence onto ``importance`` and
    left the new ``confidence`` column at the v6-migration default
    (``1.0``) and ``last_validated_at`` NULL.  When the v6 default is the
    only value we have, the row was never validated, AND ``importance``
    looks intentional (i.e. ≠ 1.0), prefer ``importance`` so legacy rows
    decay from their authored confidence rather than from a fresh ``1.0``
    baseline.  Every v6+ write stamps ``last_validated_at`` with
    ``confidence`` (the insert and each refresh), so a stamped row's
    ``confidence`` stands even at 1.0: a reused procedure decays from 1.0.
    A caller that passes no ``last_validated_at`` gets the older rule.
    This branch is a one-off compatibility shim; remove once every
    deployment is on v6+ writes (PR 6 review).

    PR 6b: promoted from the prior private ``_resolve_base_confidence``
    name (PR 5 R1 L2 + R2 M2) so :mod:`agents.memory.eviction` consumes
    it through a public symbol rather than a cross-module private
    import.  The private alias below is retained for the v0.3.x series
    so any out-of-tree caller pinned to the underscore name keeps
    working; remove in v0.4.0.
    """
    if confidence is None:
        return float(importance if importance is not None else 1.0)
    if (
        confidence == 1.0
        and last_validated_at is None
        and importance is not None
        and importance != 1.0
    ):
        return float(importance)
    return float(confidence)


# Backwards-compat private alias (PR 5 R1 L2: scheduled for removal in v0.4.0).
_resolve_base_confidence = resolve_base_confidence
