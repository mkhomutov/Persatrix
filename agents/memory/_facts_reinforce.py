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
from collections.abc import Iterable, Iterator
from typing import TYPE_CHECKING

from ._facts_audit import emit_audit as _emit_audit

if TYPE_CHECKING:
    import aiosqlite

__all__ = ["mark_recalled_for_agent"]


# SQLite caps host parameters per prepared statement at
# ``SQLITE_MAX_VARIABLE_NUMBER`` — 999 on builds older than v3.32,
# 32 766 after.  The reinforcement UPDATE binds ``(timestamp,
# agent_id, *ids)``, so the per-statement id budget is the cap minus
# the two fixed binds.  900 stays clear of the conservative pre-3.32
# limit with headroom and keeps each statement small.  Today's only
# production caller (``memory_context._inject_memory_context``) passes
# ≤40 ids — orders of magnitude below this — but the helper accepts an
# arbitrary ``Iterable[str]`` and is reachable from the future RFC 0013
# erasure backfill and RFC 0008 calibration paths.  (PR #342
# second-pass review DR2-N-3.)
_MAX_IDS_PER_UPDATE = 900


def _chunked(ids: list[str], size: int) -> Iterator[list[str]]:
    """Yield ``ids`` in contiguous slices of at most ``size`` elements."""
    for start in range(0, len(ids), size):
        yield ids[start : start + size]


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

    IN-list chunking (PR #342 second-pass review DR2-N-3)
    -----------------------------------------------------
    The ``fact_id`` list is split into :data:`_MAX_IDS_PER_UPDATE`
    chunks, one UPDATE per chunk, so an arbitrarily large id list
    cannot breach SQLite's per-statement host-parameter cap.  All
    chunks share a single trailing ``commit`` so the reinforcement
    write lands atomically.

    Audit emission (PR #342 second-pass review DR2-N-2)
    ---------------------------------------------------
    A non-empty call emits one ``fact.recalled`` RFC 0026 §G audit
    record after commit, naming every requested ``fact_id`` — once
    per call, not per id, so audit volume stays bounded.  Without it
    the audit log was blind to reinforcement even though
    MT-MEMORY-005 leg-failure analysis is one of the named consumers.

    Commit cost (PR #342 second-pass review DR2-N-8)
    ------------------------------------------------
    ``FactStore`` exposes no transaction-scope manager, so every write
    auto-commits — this helper included.  The reinforcement commit
    therefore lands once per ``_inject_memory_context`` call, after the
    facts section is staged in working memory but before the caller
    builds the LLM prompt.  Reviewed against the MQ-12 per-turn-cost
    budget: a single small UPDATE + commit is in the noise floor next
    to the LLM round-trip that immediately follows, so no
    ``FactStore.transaction()`` manager is introduced here — adding one
    would be speculative.  Revisit only if a future trace shows the
    commit on the hot path.
    """
    ids = list(fact_ids)
    if not ids:
        return
    timestamp = at if at is not None else time.time()
    for chunk in _chunked(ids, _MAX_IDS_PER_UPDATE):
        placeholders = ",".join("?" for _ in chunk)
        await db.execute(
            f"UPDATE facts "  # noqa: S608 — placeholders are literal '?'.
            f"SET last_recalled_at = MAX(COALESCE(last_recalled_at, 0), ?) "
            f"WHERE agent_id = ? AND fact_id IN ({placeholders})",
            (timestamp, agent_id, *chunk),
        )
    await db.commit()

    # RFC 0026 §G audit emission — after commit so the log cannot
    # record a write that did not happen.  One record per call (the
    # full id list rides as a field) keeps audit volume bounded.
    _emit_audit(
        "fact.recalled", agent_id=agent_id, fact_ids=ids, at=timestamp,
    )
