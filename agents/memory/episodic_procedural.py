"""
Procedural-tier read/write helpers for ``EpisodicMemory`` (RFC 0008 PR 5).

Extracted from :mod:`agents.memory.episodic` so the SQLite-write surface
in that module stays under the 500-line repo soft cap (see
:mod:`scripts.checks.file_size`).  The procedural tier reuses the
``episodes`` table — procedural rows are episodic rows that carry a
``procedure:KEY`` tag in ``tags_json`` and populate the ``confidence`` /
``last_validated_at`` columns added by migration v6.

The helpers are plain coroutines (not methods) so the dispatching
:class:`EpisodicMemory` class can keep its own surface tight; the
public façade is still ``EpisodicMemory.recall_procedures`` /
``EpisodicMemory.refresh_confidence``, which delegate here.
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass
from typing import Any

import aiosqlite

from ._session_filter import session_in_clause as _session_in_clause
from .decay import (
    DEFAULT_C_MIN,
    DEFAULT_LAMBDA_PER_DAY,
    compute_decayed_confidence,
)

# PR 6b review Should-Fix #1: defence-in-depth log-safety on the
# ``stale_memory_injection`` warn-log ``key`` field.  Procedural rows
# written *before* PR 6b's facade validator landed could carry
# arbitrary characters (the prior validator only rejected empty /
# whitespace keys), so the value pulled out of ``tags_json`` is not
# guaranteed to be control-char-free even though every *new* row is.
# ``_log_safety`` is dependency-free and explicitly anticipates this
# callsite in its module docstring.
#
# The import is *lazy* (inside :func:`recall_procedures`) because
# importing it at module load-time would trigger
# ``agents/sub_agents/__init__.py`` → spawner → ``agents.base`` →
# ``agents.memory`` → this module — a circular import while
# ``agents.base`` is still being initialised.  Using
# ``importlib.import_module`` inside the warn-log path side-steps
# the cycle and keeps the runtime cost out of the hot path (the
# import is cached after first use).

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
    legacy-row compatibility shim in :func:`recall_procedures`);
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
) -> list[ProcedureRecallEntry]:
    """Return procedural entries with read-time confidence decay applied.

    See :meth:`EpisodicMemory.recall_procedures` for full parameter
    documentation — this helper exists only to keep the SQL out of
    ``episodic.py``.

    Procedural rows are identified by the ``procedure:`` tag prefix
    written by :meth:`MemoryStore.store_procedure`.  Each row's decayed
    confidence is computed via
    :func:`~agents.memory.decay.compute_decayed_confidence` against the
    row's ``last_validated_at`` (or ``created_at`` when never
    validated).  Rows whose decayed value is below ``c_min`` are
    filtered out before the ``limit`` slice.

    PR 6b (PR 5 R1 S3 + Info-3): the SQL WHERE now pre-filters rows
    whose anchor age exceeds ``t_max = -ln(c_min) / lambda_per_day``
    seconds-equivalent so the application-side decay loop does not have
    to walk obviously-stale rows.  When ``lambda_per_day == 0`` (decay
    disabled) ``t_max`` is infinite so the cutoff is omitted.  The SQL
    ``LIMIT`` is also applied: the in-Python decay can still reduce the
    set further (a row's decayed confidence may dip below ``c_min``
    even though its anchor is within ``t_max`` due to a non-1.0 base
    confidence), but the SQL bound is a generous over-fetch
    (``2 * limit``) so the loop terminates in O(limit) rows for
    typical workloads while preserving correctness in the edge case.

    PR 6b (PR 5 R2 Mi2): when ``stale_threshold`` is supplied, this
    helper emits a ``stale_memory_injection`` structured log for each
    admitted entry whose decayed confidence falls in
    ``[c_min, stale_threshold)``.  The log lives next to the decayed
    value that triggers it; the facade mixin no longer wraps the
    return value to do this.  ``stale_threshold == None`` disables
    the alert (used by callers like the eviction pass that do not
    want a warn-log per row).
    """
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
    # NB: each appended fragment owns its own leading-space prefix, so
    # ``sql_base`` does NOT carry a trailing space — keeps the composed
    # SQL free of doubled whitespace (PR #451 deep-review nit).
    sql_base = (
        "SELECT id, summary, tags_json, confidence, "
        "last_validated_at, created_at, importance "
        "FROM episodes WHERE agent_id = ? "
        "AND tags_json LIKE '%\"procedure:%'"
    )
    params: list[Any] = [agent_id]
    # RFC 0031 Phase 2 PR 4: session filter is applied verbatim on the
    # ``session_id`` column (the procedural tier reuses the ``episodes``
    # table, so the §D predicate composes with the existing tag-based
    # WHERE).  ``session_list=None`` is the ``"*"`` sentinel and produces
    # an empty fragment.  Carve-out is already in the resolved list.
    if session_list is not None:
        session_clause, session_params = _session_in_clause(
            session_list, column="session_id",
        )
        sql_base += session_clause
        params.extend(session_params)
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
        sql = (
            sql_base + " AND summary LIKE ? ESCAPE '\\'"
            " ORDER BY created_at DESC LIMIT ?"
        )
        params.extend([f"%{_escape_like(query)}%", max(limit * 2, limit)])
    else:
        sql = sql_base + " ORDER BY created_at DESC LIMIT ?"
        params.append(max(limit * 2, limit))
    async with db.execute(sql, tuple(params)) as cursor:
        rows = list(await cursor.fetchall())
    out: list[ProcedureRecallEntry] = []
    for row in rows:
        row_id, summary, tags_json, confidence, last_val, created_at, importance = row
        base_conf = resolve_base_confidence(confidence, importance)
        anchor = last_val if last_val is not None else created_at
        age = timestamp - float(anchor)
        decayed = compute_decayed_confidence(
            base_conf, age, lambda_per_day=lambda_per_day,
        )
        if decayed < c_min:
            continue
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
        out.append(entry)
        if len(out) >= limit:
            break
    return out


async def refresh_confidence(
    db: aiosqlite.Connection,
    agent_id: str,
    key: str,
) -> bool:
    """Mark every procedure row tagged ``procedure:{key}`` as freshly validated.

    Sets ``confidence = 1.0`` and ``last_validated_at = time.time()`` on
    the matching rows for ``agent_id``.  Returns ``True`` when at least
    one row was updated.

    Implements the "Confidence refresh on successful reuse" contract
    from RFC 0008 §G — :meth:`MemoryStore.store_procedure` invokes it
    automatically when a re-store hits an existing key.
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
    cursor = await db.execute(
        "UPDATE episodes SET confidence = 1.0, last_validated_at = ? "
        "WHERE agent_id = ? AND tags_json LIKE ? ESCAPE '\\'",
        (time.time(), agent_id, pattern),
    )
    await db.commit()
    return (cursor.rowcount or 0) > 0


def resolve_base_confidence(
    confidence: float | None,
    importance: float | None,
) -> float:
    """Return the ``c_0`` value to feed into the decay formula.

    Legacy PR 2 procedural rows wrote confidence onto ``importance`` and
    left the new ``confidence`` column at the v6-migration default
    (``1.0``).  When the v6 default is the only value we have AND
    ``importance`` looks intentional (i.e. ≠ 1.0), prefer ``importance``
    so legacy rows decay from their authored confidence rather than
    from a fresh ``1.0`` baseline.  New writers populate both columns
    consistently so this branch is a one-off compatibility shim;
    remove once every deployment is on v6+ writes (PR 6 review).

    PR 6b: promoted from the prior private ``_resolve_base_confidence``
    name (PR 5 R1 L2 + R2 M2) so :mod:`agents.memory.eviction` consumes
    it through a public symbol rather than a cross-module private
    import.  The private alias below is retained for the v0.3.x series
    so any out-of-tree caller pinned to the underscore name keeps
    working; remove in v0.4.0.
    """
    if confidence is None:
        return float(importance if importance is not None else 1.0)
    if confidence == 1.0 and importance is not None and importance != 1.0:
        return float(importance)
    return float(confidence)


# Backwards-compat private alias (PR 5 R1 L2: scheduled for removal in v0.4.0).
_resolve_base_confidence = resolve_base_confidence
