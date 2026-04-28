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
import time
from dataclasses import dataclass
from typing import Any

import aiosqlite

from .decay import (
    DEFAULT_C_MIN,
    DEFAULT_LAMBDA_PER_DAY,
    compute_decayed_confidence,
)

__all__ = [
    "ProcedureRecallEntry",
    "extract_procedure_key",
    "recall_procedures",
    "refresh_confidence",
]


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
    tag by :meth:`MemoryFacade.store_procedure`, but the helper is
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
    now: float | None = None,
) -> list[ProcedureRecallEntry]:
    """Return procedural entries with read-time confidence decay applied.

    See :meth:`EpisodicMemory.recall_procedures` for full parameter
    documentation — this helper exists only to keep the SQL out of
    ``episodic.py``.

    Procedural rows are identified by the ``procedure:`` tag prefix
    written by :meth:`MemoryFacade.store_procedure`.  Each row's decayed
    confidence is computed via
    :func:`~agents.memory.decay.compute_decayed_confidence` against the
    row's ``last_validated_at`` (or ``created_at`` when never
    validated).  Rows whose decayed value is below ``c_min`` are
    filtered out before the ``limit`` slice.
    """
    timestamp = now if now is not None else time.time()
    # Procedural rows carry a tag formatted ``procedure:{key}`` — see
    # ``MemoryFacade.store_procedure``.  ``tags_json`` stores the tag
    # list as a JSON array, so the LIKE pattern matches the tag
    # substring verbatim regardless of array position.  Phase 5 uses
    # ``LIKE`` rather than FTS5 so a procedure key with punctuation
    # can be matched verbatim by the caller without FTS5-sanitising.
    sql_base = (
        "SELECT id, summary, tags_json, confidence, "
        "last_validated_at, created_at, importance "
        "FROM episodes WHERE agent_id = ? "
        "AND tags_json LIKE '%\"procedure:%' "
    )
    if query:
        sql = sql_base + "AND summary LIKE ? ORDER BY created_at DESC"
        params: tuple[Any, ...] = (agent_id, f"%{query}%")
    else:
        sql = sql_base + "ORDER BY created_at DESC"
        params = (agent_id,)
    async with db.execute(sql, params) as cursor:
        rows = list(await cursor.fetchall())
    out: list[ProcedureRecallEntry] = []
    for row in rows:
        row_id, summary, tags_json, confidence, last_val, created_at, importance = row
        base_conf = _resolve_base_confidence(confidence, importance)
        anchor = last_val if last_val is not None else created_at
        age = timestamp - float(anchor)
        decayed = compute_decayed_confidence(
            base_conf, age, lambda_per_day=lambda_per_day,
        )
        if decayed < c_min:
            continue
        tags = json.loads(tags_json) if tags_json else []
        out.append(
            ProcedureRecallEntry(
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
        )
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
    from RFC 0008 §G — :meth:`MemoryFacade.store_procedure` invokes it
    automatically when a re-store hits an existing key.
    """
    if not key or not key.strip():
        raise ValueError("key must not be empty")
    # Quote the key inside the LIKE pattern so a key with embedded
    # ``%`` cannot widen the match.  ``%"procedure:KEY"%`` matches the
    # JSON-array element exactly because ``json.dumps`` always emits
    # the value with surrounding double quotes.
    pattern = f'%"procedure:{key}"%'
    cursor = await db.execute(
        "UPDATE episodes SET confidence = 1.0, last_validated_at = ? "
        "WHERE agent_id = ? AND tags_json LIKE ?",
        (time.time(), agent_id, pattern),
    )
    await db.commit()
    return (cursor.rowcount or 0) > 0


def _resolve_base_confidence(
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
    """
    if confidence is None:
        return float(importance if importance is not None else 1.0)
    if confidence == 1.0 and importance is not None and importance != 1.0:
        return float(importance)
    return float(confidence)
