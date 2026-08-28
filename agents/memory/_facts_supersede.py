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
live row per ``(agent_id, subject, predicate, session_id)`` key, with
the row carrying the greatest ``asserted_at`` winning.  The
``session_id`` predicate is the RFC 0031 Phase 2 PR 5 §F amendment —
each session keeps its own truth about ``(subject, predicate)`` rather
than one global truth; cross-session writes never retroactively
contaminate another session's view (`ISSUE-0079
<../../docs/issues/ISSUE-0079-cross-session-supersede-not-scoped.md>`_).
The ``legacy`` carve-out participates asymmetrically, mirroring the §D
recall filter (``session_id IN (active, legacy)``) — a write tagged
with the active session can supersede an older ``legacy`` row (a pre-RFC
fact the active session has reasserted) and a ``legacy`` write can
supersede its own predecessors, but a ``legacy`` write cannot reach
across to a named session ("not vice versa") and a non-legacy write
cannot reach across to another non-legacy session.  Two cases:

* **Older / equal live rows** (``asserted_at <= new.asserted_at`` in
  the same session *or* the ``legacy`` carve-out) are marked superseded
  by the new row.  Pulling all qualifying rows cleans up older-side
  legacy multi-live invariant violations from the pre-PR-5a ``<``
  semantics on the same write, not just the most recent one.  Newer-side
  legacy violations (multiple strictly-newer
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
The choice is deterministic at the storage layer.  Since the v0.3.15
``(principal, speaker, scope)`` re-key (PR #846), equal timestamps ARE
reachable in the production write path: a room-wide close stamps every
sibling record with ONE ``closed_at`` instant, so two siblings
asserting the same ``(subject, predicate)`` tie here and the later
Phase-2 arrival wins — an ordering the RFC 0020 §G amendment states,
and which the residuals PR 4 speaker binding is expected to
disambiguate properly.  The rule also still covers fixtures, the OQ #9
operator-seeded path, and the future RFC 0013 erasure backfill.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from ..session_id import LEGACY_SESSION_ID
from ._facts_audit import emit_audit

if TYPE_CHECKING:
    import aiosqlite

__all__ = ["SupersessionResult", "apply_supersession", "retract_fact"]


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
    session_id: str,
    principal_id: str,
    epoch_id: str,
) -> SupersessionResult:
    """Sweep older + newer live rows for the symmetric latest-wins chain.

    Called by :meth:`agents.memory.facts.FactStore.store` immediately
    after the INSERT and before the per-statement ``commit``.  The
    helper issues the ``UPDATE`` writes itself but defers the commit to
    the caller so the INSERT and the chain land atomically.

    ``session_id`` (RFC 0031 Phase 2 PR 5 — RFC 0026 §F amendment): the
    supersede chain is keyed on ``(agent_id, subject, predicate,
    session_id)`` with the ``legacy`` carve-out folded in asymmetrically.
    A write in session ``run-b`` cannot supersede a row in session
    ``run-a`` — each named session has its own latest-wins chain on the
    same ``(subject, predicate)`` (`ISSUE-0079
    <../../docs/issues/ISSUE-0079-cross-session-supersede-not-scoped.md>`_).

    ``principal_id`` (ISSUE-0081 PR 3) adds the orthogonal tenant axis to
    the chain key with **strict equality** (no carve-out — the principal
    axis has none): both sweeps require ``principal_id = ?`` so a write by
    one tenant can never supersede — and thereby silently retract — a row
    owned by another, even when subject / predicate / session collide.

    ``epoch_id`` (ISSUE-0085 PR 3) adds the run/test-isolation axis to the
    chain key with the same **strict equality** (no carve-out, no ``"*"``):
    both sweeps require ``epoch_id = ?`` so a write under a fresh epoch can
    never supersede a prior run's row.

    The carve-out is asymmetric, matching the §D recall filter
    (``session_id IN (active, legacy)``):

    * **Older sweep** spans ``(session_id, legacy)`` — an active-session
      write absorbs older ``legacy`` predecessors (the upgrade hot-path:
      a pre-RFC fact reasserted under a pinned session), so the active
      session sees a single live row rather than the legacy row and the
      reassertion both surfacing through the carve-out.
    * **Newer dominator** stays exact (``session_id`` only) — a
      ``legacy`` row never supersedes a named session's write ("but not
      vice versa").

    Consequence (accepted tradeoff): because ``superseded_by`` is a
    single global pointer, an active session absorbing a ``legacy`` row
    also removes it from *other* sessions' carve-out view.  This is the
    latest-asserted-wins contract applied across the shared pre-RFC
    baseline; isolation between two *named* sessions is unaffected.
    The residual newer-side case (an active write that is *older* than a
    live ``legacy`` row) is left unhealed in-band, symmetric to the
    same-session newer-side caveat documented on the module.
    """
    older_sessions: tuple[str, ...] = (
        (session_id,)
        if session_id == LEGACY_SESSION_ID
        else (session_id, LEGACY_SESSION_ID)
    )
    older_placeholders = ",".join("?" for _ in older_sessions)
    async with db.execute(
        f"""
        SELECT fact_id FROM facts
        WHERE agent_id = ?
          AND subject = ?
          AND predicate = ?
          AND session_id IN ({older_placeholders})
          AND principal_id = ?
          AND epoch_id = ?
          AND superseded_by IS NULL
          AND asserted_at <= ?
          AND fact_id != ?
        """,  # noqa: S608 — placeholders only; values bound below.
        (
            agent_id, subject, predicate, *older_sessions,
            principal_id, epoch_id, asserted_at, new_fact_id,
        ),
    ) as cursor:
        older_rows = await cursor.fetchall()
    older_fact_ids: tuple[str, ...] = tuple(row[0] for row in older_rows)

    async with db.execute(
        """
        SELECT fact_id FROM facts
        WHERE agent_id = ?
          AND subject = ?
          AND predicate = ?
          AND session_id = ?
          AND principal_id = ?
          AND epoch_id = ?
          AND superseded_by IS NULL
          AND asserted_at > ?
        ORDER BY asserted_at DESC
        LIMIT 1
        """,
        (
            agent_id, subject, predicate, session_id, principal_id,
            epoch_id, asserted_at,
        ),
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


async def retract_fact(
    db: aiosqlite.Connection,
    *,
    agent_id: str,
    fact_id: str,
    by_fact_id: str,
    principal_id: str,
    epoch_id: str,
) -> bool:
    """Explicitly point ``fact_id``'s ``superseded_by`` at ``by_fact_id``.

    The manual-retract primitive behind
    :meth:`agents.memory.facts.FactStore.supersede` — for callers (PR 4 +
    future RFC 0027 consolidation) that retract a fact without writing a
    successor of identical ``(subject, predicate)``.  Only a live row
    (``superseded_by IS NULL``) owned by ``agent_id`` is touched.

    ``principal_id`` (ISSUE-0081 PR 3 review follow-up) and ``epoch_id``
    (ISSUE-0085 PR 3) scope the write with the same strict equality
    :func:`apply_supersession` uses, so neither a foreign tenant nor a
    fresh epoch can retract another's fact by id even though both rows
    share the ``agent_id``.  Commits itself and, on a true result,
    emits the RFC 0026 §G ``fact.supersede`` audit record — mirroring the
    self-contained-primitive precedent of
    :func:`agents.memory._facts_reinforce.mark_recalled_for_agent`.
    Returns ``True`` iff a live row was updated.
    """
    cursor = await db.execute(
        "UPDATE facts SET superseded_by = ? "
        "WHERE fact_id = ? AND agent_id = ? "
        "AND superseded_by IS NULL "
        "AND principal_id = ? "
        "AND epoch_id = ?",
        (by_fact_id, fact_id, agent_id, principal_id, epoch_id),
    )
    await db.commit()
    retracted = (cursor.rowcount or 0) > 0
    if retracted:
        emit_audit(
            "fact.supersede", agent_id=agent_id,
            superseded_fact_id=fact_id, by_fact_id=by_fact_id,
        )
    return retracted
