"""Person-identity write-through for the ``store_note`` tool.

RFC 0031 amendment (F-7 Option D, ISSUE-0093) **PR D2** — a
``store_note(topic="contact:<id>")`` call (the one the model already
makes per ``memory-tool-usage.md``) additionally upserts structured
person identity onto the cross-room relationship tier, so a name learned
in one room surfaces in every room for that person.

Factored out of :mod:`agents.tools.builtin` to keep that module under
the 500-line review cap and to keep the write-through (parse → bind type
→ upsert) testable as one unit independent of the broader tool registry.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..memory._notes_recall import CONTACT_TOPIC_PREFIX
from ..memory.identity_parse import parse_identity_fields
from ..sender_type import current_sender_type

if TYPE_CHECKING:
    from ..memory.relationship import RelationshipMemory

logger = logging.getLogger(__name__)

__all__ = ["maybe_write_through_identity"]


async def maybe_write_through_identity(
    relationship: RelationshipMemory | None,
    topic: str,
    content: str,
) -> None:
    """Upsert cross-room identity when ``topic`` is a ``contact:<id>`` note.

    A no-op when there is no relationship handle (non-persona callers /
    the pre-wiring path) or the topic is not a contact note.  This is a
    *dual-write* during the D2 transition — the caller still writes the
    legacy room-scoped note; D3 drops it once cross-room recall is
    verified live.

    The free-text ``content`` is structured by the pure, deterministic
    :func:`agents.memory.identity_parse.parse_identity_fields` (never an
    LLM call — decision D-1).  The ``other_participant_type`` is the
    inbound sender's, bound task-locally for this event by
    :mod:`agents.sender_type`, so identity lands on the same relationship
    row the recall side later queries.

    Best-effort: an identity failure must never fail the ``store_note``
    tool the model depends on, so it is logged and swallowed.
    """
    if relationship is None or not topic.startswith(CONTACT_TOPIC_PREFIX):
        return
    other_id = topic[len(CONTACT_TOPIC_PREFIX):].strip()
    fields = parse_identity_fields(content)
    if not other_id or not fields:
        return
    try:
        await relationship.upsert_identity(
            other_id, fields,
            other_participant_type=current_sender_type(),
        )
    except Exception:
        logger.warning(
            "Identity write-through failed for topic %s", topic, exc_info=True,
        )
