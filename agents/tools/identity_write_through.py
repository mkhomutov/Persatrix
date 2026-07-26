"""Person-identity write-through for the ``store_note`` tool.

RFC 0031 amendment (F-7 Option D, ISSUE-0093) **PR D3** — a
``store_note(topic="contact:<id>")`` call (the one the model already
makes per ``memory-tool-usage.md``) upserts structured person identity
onto the cross-room relationship tier, so a name learned in one room
surfaces in every room for that person.  D2 *dual-wrote* (relationship
identity *and* a legacy room-scoped note); D3 retires the note write —
identity now lives on the relationship tier alone, so the cross-room
recall seam cannot recur by construction.

Factored out of :mod:`agents.tools.builtin` to keep that module under
the 500-line review cap and to keep the write-through (parse → bind type
→ upsert) testable as one unit independent of the broader tool registry.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..acting_classification import current_acting_classification
from ..memory.identity_parse import parse_identity_fields
from ..sender_type import current_sender_type

if TYPE_CHECKING:
    from ..memory.relationship import RelationshipMemory

logger = logging.getLogger(__name__)

__all__ = ["CONTACT_TOPIC_PREFIX", "maybe_write_through_identity"]

#: Person-keyed note topics whose subject is a *person* (cross-room).  The
#: convention the ``memory-tool-usage.md`` prompt instructs the model to
#: use (``contact:<participant_id>``); the sole trigger for routing a
#: ``store_note`` call onto the cross-room relationship identity tier.
#: Owned here post-D3 — the notes-recall path no longer special-cases it.
CONTACT_TOPIC_PREFIX = "contact:"


async def maybe_write_through_identity(
    relationship: RelationshipMemory | None,
    topic: str,
    content: str,
) -> bool:
    """Upsert cross-room identity when ``topic`` is a ``contact:<id>`` note.

    Returns ``True`` when identity was written to the relationship tier —
    the signal to the caller that **no room-scoped note should be written**
    (D3 retires the legacy note write; identity lives on the relationship
    tier alone).  On this path any ``tags`` the caller held are intentionally
    dropped: the identity tier has no tag field, and re-adding a note to
    carry them would reopen the cross-room seam D3 closes.  Returns ``False``
    to mean "fall back to the note write", which happens when:

    * there is no relationship handle (non-persona callers / pre-wiring),
    * the topic is not a contact note,
    * the acting classification outranks ``internal`` (RFC 0037 §C —
      the cross-room identity tier must not learn from a
      ``restricted``/``secret`` turn; the room-scoped note IS the
      intended destination, not merely a safety net, on this branch),
    * the content carries no structured identity to store, or
    * the identity upsert raised.

    The fallback is the safety net: when identity cannot be persisted, the
    contact content is not silently dropped — the caller writes it as a
    room-scoped note instead, exactly as it did before this feature.

    The free-text ``content`` is structured by the pure, deterministic
    :func:`agents.memory.identity_parse.parse_identity_fields` (never an
    LLM call — decision D-1).  The ``other_participant_type`` is the
    inbound sender's, bound task-locally for this event by
    :mod:`agents.sender_type`, so identity lands on the same relationship
    row the recall side later queries.

    **Invariant — the note's subject is the inbound sender.**  The row
    identity lands on is keyed by ``(other_id, other_participant_type)``:
    ``other_id`` comes from the *topic*, but the type comes from the
    *current event's* sender.  These agree only when the model writes the
    ``contact:<id>`` note about the peer it is currently talking to — what
    ``memory-tool-usage.md`` instructs.  A note about a *third party* of a
    different participant type (e.g. an agent peer named while talking to a
    user) would be written under the sender's type, and the cross-room read
    for that third party would miss it.  We deliberately do not infer the
    type from ``<id>`` — there is no reliable id→type convention — so the
    prompt-enforced same-subject contract is what this relies on.
    """
    if relationship is None or not topic.startswith(CONTACT_TOPIC_PREFIX):
        return False
    # RFC 0037 §C (v0.3.12 PR 3): the relationship identity tier is
    # deliberately cross-room and carries no protection level — an ungated
    # egress surface (a role learned in a ``secret`` channel would render
    # into *every* room's prompt).  The smallest rule that preserves the
    # structural guarantee: the write-through proceeds only when the acting
    # classification ranks ≤ ``internal``; in a ``restricted``/``secret``
    # turn the ``False`` return routes the content to the room-scoped note
    # fallback below, which is stamped and gated.  The acting level rides
    # the task-local :mod:`agents.acting_classification` axis (the
    # ``sender_type`` precedent); the rank comparison is the named §A
    # helper, imported lazily because this executor-side module must not
    # hard-depend on the persona subpackage (the ``persona.py``
    # ``resolve_session_id_and_log`` cycle-break precedent) — by tool-call
    # time the persona runtime is fully imported.
    from ..persona_runtime.classification import acting_at_or_below_internal

    acting = current_acting_classification()
    if not acting_at_or_below_internal(acting):
        logger.info(
            "Identity write-through withheld for topic %s: acting "
            "classification %r outranks 'internal'; falling back to a "
            "room-scoped note (RFC 0037 §C)", topic, acting,
        )
        return False
    other_id = topic[len(CONTACT_TOPIC_PREFIX):].strip()
    fields = parse_identity_fields(content)
    if not other_id or not fields:
        return False
    try:
        await relationship.upsert_identity(
            other_id, fields,
            other_participant_type=current_sender_type(),
        )
    except Exception:
        # Fall back to the note write so the model's data is never lost —
        # an identity failure must never silently drop what it stored.
        logger.warning(
            "Identity write-through failed for topic %s; "
            "falling back to a room-scoped note", topic, exc_info=True,
        )
        return False
    return True
