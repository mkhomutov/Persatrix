"""Reinforcement helper for :class:`agents.memory.facts.FactStore`
(RFC 0026 PR 4).

Split out of :mod:`agents.memory.facts` so the parent module stays
under the project's 500-line review-friendly cap (see
``scripts/checks/file_size.py``) — same precedent as the
:mod:`_facts_audit` split.  :func:`mark_recalled_for_agent` is the
single write surface for the ``last_recalled_at`` column; the
``FactStore`` method delegates here so the storage primitive's audit /
write contracts stay grouped on the class but the bulky parameterised
UPDATE lives in this helper.

The reinforcement write composes with :doc:`RFC 0008 §G
<../../docs/rfcs/0008-agent-memory-context-optimization>` decay /
validation via the same scoring seam.  The calibration formula lands
in :doc:`RFC 0008 calibration review
<../../docs/rfcs/0008-calibration-review>`; this primitive ships only
the column write.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite

__all__ = ["mark_recalled_for_agent"]


async def mark_recalled_for_agent(
    db: aiosqlite.Connection,
    agent_id: str,
    fact_ids: Iterable[str],
    *,
    at: float | None = None,
) -> None:
    """Write ``last_recalled_at`` on every named fact_id owned by ``agent_id``.

    Per-agent ACL (RFC 0008 §H): the UPDATE is scoped to ``agent_id``
    so a stray fact_id from another tenant's store is silently
    skipped.  Empty / missing fact_ids no-op without a DB round-trip;
    the recall-time reinforcement path must never raise.  ``at``
    defaults to :func:`time.time`.

    Monotonicity (PR #342 review N-1)
    ---------------------------------
    ``last_recalled_at`` is monotone non-decreasing.  The UPDATE
    clamps the column to ``MAX(COALESCE(last_recalled_at, 0), ?)``
    so an older ``at`` argument never clobbers a newer existing
    value.  Composes with :doc:`RFC 0008 §G
    <../../docs/rfcs/0008-agent-memory-context-optimization>` decay /
    validation on a "newest recall wins" basis; the failure modes a
    naive overwrite would expose are NTP step-back, operators
    replaying older interactions via the OQ #9 seeded-facts path,
    and test fixtures that pass an explicit ``at`` out of order.
    ``COALESCE(..., 0)`` matters because the column starts ``NULL``
    and SQLite's ``MAX(NULL, x) = NULL``, which would silently no-op
    the first call.
    """
    ids = list(fact_ids)
    if not ids:
        return
    timestamp = at if at is not None else time.time()
    placeholders = ",".join("?" for _ in ids)
    await db.execute(
        f"UPDATE facts "  # noqa: S608 — placeholders are literal '?'.
        f"SET last_recalled_at = MAX(COALESCE(last_recalled_at, 0), ?) "
        f"WHERE agent_id = ? AND fact_id IN ({placeholders})",
        (timestamp, agent_id, *ids),
    )
    await db.commit()
