"""Subject-erasure helper for :class:`agents.memory.facts.FactStore`
(RFC 0026 §H / RFC 0013 §C).

Split out of :mod:`agents.memory.facts` so the parent module stays
under the project's 500-line review-friendly cap (see
``scripts/checks/file_size.py``) — same precedent as the
:mod:`_facts_audit`, :mod:`_facts_reinforce`, and :mod:`_facts_supersede`
splits.  :func:`delete_by_subject` is the single GDPR-traversal point
for the ``facts`` tier; the ``FactStore`` method delegates here.

RFC 0013's ``SubjectErasure`` (target v0.5.0) will wire this into the
umbrella ``records_deleted`` audit map.  Without the primitive the first
GDPR / CCPA request after v0.3.1 ships would silently miss extracted
facts — see `RFC 0026 §H
<../../docs/rfcs/0026-declarative-facts-tier.md#h-erasure-rfc-0013>`_.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .fact_predicates import canonicalize_subject

if TYPE_CHECKING:
    import aiosqlite

__all__ = ["delete_by_subject"]


async def delete_by_subject(
    db: aiosqlite.Connection,
    agent_id: str,
    subject_id: str,
) -> dict[str, int]:
    """Erase every fact tied to ``subject_id`` — RFC 0013 §C / RFC 0026 §H.

    Traverses **both** the ``subject`` column (facts *about* the
    subject) and the ``source_interaction_id`` column (facts
    *extracted during* an interaction belonging to the subject —
    even if the declared subject is someone else).

    Return shape: two **disjoint** row-counts whose sum is the total
    number of fact rows erased.  The split exists so the audit log can
    show whether erasure landed via the declared ``subject`` traversal
    or via the ``source_interaction_id`` reverse-edge traversal.  A row
    matching both columns is counted once, in the ``by_subject`` bucket
    (the first DELETE removes the row; the second DELETE sees no
    match).  This is **not** a per-column match counter — it is a
    per-row attribution counter biased toward the declared-subject
    column.  Pinned by
    :class:`tests.unit.python.test_fact_store.TestDeleteBySubject`.

    Per-agent ACL — RFC 0008 §H.  Both DELETEs are scoped to ``agent_id``,
    so an erasure call from agent A cannot touch agent B's facts even
    when both stores share the same SQLite connection.

    Subject canonicalisation (PR #346 review M-1): the ``subject``
    traversal canonicalises so a mixed-case erasure hits the
    canonical rows :meth:`FactStore.store` persists;
    ``source_interaction_id`` stays raw (opaque UUIDs, not subject
    strings).  Empty subjects are rejected at the ``FactStore`` boundary
    via :func:`canonicalize_subject`'s value check.
    """
    cursor = await db.execute(
        "DELETE FROM facts WHERE agent_id = ? AND subject = ?",
        (agent_id, canonicalize_subject(subject_id)),
    )
    by_subject = cursor.rowcount
    cursor = await db.execute(
        "DELETE FROM facts WHERE agent_id = ? "
        "AND source_interaction_id = ?",
        (agent_id, subject_id),
    )
    by_source = cursor.rowcount
    await db.commit()
    return {
        "facts_deleted_by_subject": by_subject,
        "facts_deleted_by_source_interaction": by_source,
    }
