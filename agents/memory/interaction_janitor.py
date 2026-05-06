"""
Closing-state janitor + summary-text sentinels (RFC 0020 §C / PR 4).

Extracted from :mod:`agents.memory.interactions` to keep that module
under the 500-line review cap. The janitor runs independently of the
in-memory :class:`~agents.memory.interactions.InteractionTracker` and
operates entirely on the persisted ``episodes`` table; isolating it
here also keeps the :class:`~agents.memory.interactions.InteractionTracker`
free of ``aiosqlite`` imports.

Public symbols are re-exported from :mod:`agents.memory.interactions`
for backward compatibility with existing import sites.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from ..observability.metrics import current_agent_id, try_get_instruments
from .boundary_detectors import DEFAULT_CLOSING_GRACE_SEC

if TYPE_CHECKING:
    import aiosqlite

logger = logging.getLogger(__name__)


# ─── Summary text sentinels (RFC 0020 §C) ────────────────────
#
# ``SUMMARY_PENDING_TEXT`` marks a closing-state row whose LLM
# summarisation has not yet completed (e.g. the writer crashed between
# ``INSERT`` and the post-LLM ``UPDATE``).  The PR 4 janitor
# (:func:`cleanup_closing_interactions`) sweeps these rows once they
# exceed ``closing_grace_sec`` and replaces the sentinel with
# ``SUMMARY_UNAVAILABLE_TEXT``.  The sentinel must be unique enough that
# an honest LLM summary cannot collide with it; the bracketed form keeps
# it readable in operator dashboards while making accidental collisions
# implausible.
#
# ``SUMMARY_UNAVAILABLE_TEXT`` is the public fallback rendered to
# downstream consumers (relationship-memory recall, persona prompt
# assembly) when summarisation could not produce a real summary.  It is
# deliberately ASCII-only so legacy log pipelines (CP-1252, GBK) cannot
# re-encode it into ``?``.
SUMMARY_PENDING_TEXT: str = "[summary pending]"
SUMMARY_UNAVAILABLE_TEXT: str = "[interaction summary unavailable]"


async def cleanup_closing_interactions(
    db: aiosqlite.Connection,
    agent_id: str,
    *,
    grace_sec: float = DEFAULT_CLOSING_GRACE_SEC,
    now: float | None = None,
) -> int:
    """Backfill the fallback summary on stuck ``closing``-state rows.

    A ``closing``-state row is one whose ``closed_at`` column is set
    (the tracker successfully closed the interaction and the persistence
    layer wrote the row) but whose ``summary`` column still carries
    :data:`SUMMARY_PENDING_TEXT` — the marker the summariser writes
    before its LLM call.  Such a row indicates the post-LLM ``UPDATE``
    never landed (process crash, network partition, summariser timeout
    that exceeded the synchronous wait window).

    The janitor is best-effort and idempotent: it scans rows whose
    ``closed_at`` predates ``now − grace_sec`` and rewrites their
    summary to :data:`SUMMARY_UNAVAILABLE_TEXT` while incrementing the
    ``agent.interactions.summary.failed`` counter once per row.

    Returns the number of rows updated.  Callers may invoke this from
    a periodic tick or operator-driven recovery script; the function
    does not own its own scheduling.
    """
    ts = now if now is not None else time.time()
    cutoff = ts - grace_sec
    cursor = await db.execute(
        "UPDATE episodes SET summary = ? "
        "WHERE agent_id = ? AND summary = ? AND closed_at IS NOT NULL "
        "AND closed_at < ?",
        (SUMMARY_UNAVAILABLE_TEXT, agent_id, SUMMARY_PENDING_TEXT, cutoff),
    )
    updated = cursor.rowcount or 0
    if updated > 0:
        await db.commit()
        inst = try_get_instruments()
        if inst is not None:
            # PR #229 review nice-to-have #2: a single ``add(updated,
            # attrs)`` is OTel-equivalent to N ``add(1, attrs)`` calls
            # for a Counter and avoids a per-row Python loop on a
            # potentially large backfill.
            attrs = {"agent_id": current_agent_id(), "reason": "janitor"}
            inst.interactions_summary_failed.add(updated, attrs)
        logger.info(
            "Janitor backfilled %d closing-state interaction(s) for agent %s",
            updated, agent_id,
        )
    return updated


__all__ = [
    "SUMMARY_PENDING_TEXT",
    "SUMMARY_UNAVAILABLE_TEXT",
    "cleanup_closing_interactions",
]
