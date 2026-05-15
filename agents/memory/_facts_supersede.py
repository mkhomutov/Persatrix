"""Supersede-on-insert helper for :class:`agents.memory.facts.FactStore`
(RFC 0026 PR 5a — symmetric latest-asserted-wins follow-up).

Split out of :mod:`agents.memory.facts` so the parent module stays
under the project's 500-line review-friendly cap (see
``scripts/checks/file_size.py``).  Mirrors the :mod:`_facts_audit` /
:mod:`_facts_reinforce` precedent — one RFC-section-scoped helper,
imported by the parent module without exposing it to direct callers.

Symmetric latest-asserted-wins rule (RFC 0026 §F)
-------------------------------------------------
When a fact tuple is written, the storage primitive enforces a single
live row per ``(agent_id, subject, predicate)`` key, with the row
carrying the greatest ``asserted_at`` winning.  Two cases:

* **Older / equal live rows** (``asserted_at <= new.asserted_at``) are
  marked superseded by the new row.  Pulling all qualifying rows
  cleans up older-side legacy multi-live invariant violations from the
  pre-PR-5a ``<`` semantics on the same write, not just the most
  recent one.  Newer-side legacy violations (multiple strictly-newer
  live rows for the same key) are *not* healed by an in-band write —
  the forward-pass ``LIMIT 1`` only points the new row at the topmost
  dominator; the lower-but-still-newer siblings remain live alongside.
  The production extractor (PR 2) uses monotonic
  ``interaction.closed_at`` so newer-side legacy state is unreachable
  in the hot path; an explicit reassertion sweep would be needed if a
  fixture / seed path ever creates one.
* **Strictly-newer live row** (``asserted_at > new.asserted_at``)
  dominates the new row: the new row is itself marked superseded by
  that newer row.  An out-of-order older write therefore self-
  supersedes on insert rather than leaving two live rows.

Equal-timestamp ties break in favour of the later arrival (the row
being inserted), matching the PR 5a deferred-item resolution from
:doc:`docs/rfcs/0026-pr-plan.md <../../docs/rfcs/0026-pr-plan>`.
The choice is deterministic at the storage layer — the production
extractor (PR 2) uses ``interaction.closed_at`` which is monotonic
per-agent, so equal timestamps are unreachable in the production
write path; the rule exists for fixtures, the OQ #9 operator-seeded
path, and the future RFC 0013 erasure backfill where the precondition
may not hold.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    import aiosqlite

__all__ = ["SupersessionResult", "apply_supersession"]


class SupersessionResult(NamedTuple):
    """Outcome of the supersession sweep around a single :meth:`store` call.

    ``superseded_older_ids`` lists the existing live rows the new row
    marked superseded (older or equal timestamp; chain target = new
    row).  ``self_superseded_by`` is non-``None`` when a strictly-newer
    live row already existed, in which case the new row's
    ``superseded_by`` was pointed at that id.  Both fields can be
    populated for the same call (out-of-order write that bumps a
    legacy older row out of liveness *and* hits an existing newer
    dominator).
    """

    superseded_older_ids: tuple[str, ...]
    self_superseded_by: str | None


async def apply_supersession(
    db: aiosqlite.Connection,
    *,
    agent_id: str,
    subject: str,
    predicate: str,
    asserted_at: float,
    new_fact_id: str,
) -> SupersessionResult:
    """Sweep older + newer live rows for the symmetric latest-wins chain.

    Called by :meth:`agents.memory.facts.FactStore.store` immediately
    after the INSERT and before the per-statement ``commit``.  The
    helper issues the ``UPDATE`` writes itself but defers the commit to
    the caller so the INSERT and the chain land atomically.
    """
    async with db.execute(
        """
        SELECT fact_id FROM facts
        WHERE agent_id = ?
          AND subject = ?
          AND predicate = ?
          AND superseded_by IS NULL
          AND asserted_at <= ?
          AND fact_id != ?
        """,
        (agent_id, subject, predicate, asserted_at, new_fact_id),
    ) as cursor:
        older_rows = await cursor.fetchall()
    older_fact_ids: tuple[str, ...] = tuple(row[0] for row in older_rows)

    async with db.execute(
        """
        SELECT fact_id FROM facts
        WHERE agent_id = ?
          AND subject = ?
          AND predicate = ?
          AND superseded_by IS NULL
          AND asserted_at > ?
        ORDER BY asserted_at DESC
        LIMIT 1
        """,
        (agent_id, subject, predicate, asserted_at),
    ) as cursor:
        newer_row = await cursor.fetchone()
    self_superseded_by: str | None = newer_row[0] if newer_row else None

    if older_fact_ids:
        placeholders = ",".join("?" for _ in older_fact_ids)
        await db.execute(
            f"UPDATE facts SET superseded_by = ? "  # noqa: S608 — '?' literals.
            f"WHERE agent_id = ? AND fact_id IN ({placeholders})",
            (new_fact_id, agent_id, *older_fact_ids),
        )

    if self_superseded_by is not None:
        await db.execute(
            "UPDATE facts SET superseded_by = ? "
            "WHERE fact_id = ? AND agent_id = ?",
            (self_superseded_by, new_fact_id, agent_id),
        )

    return SupersessionResult(
        superseded_older_ids=older_fact_ids,
        self_superseded_by=self_superseded_by,
    )
