"""Cross-room contact-note recall for the persona memory context.

Factored out of :mod:`agents.persona_runtime.memory_context` to keep that
module under the 500-line code cap and to mirror the other tier helpers
(:mod:`relationship_section` / :mod:`facts_section`).

RFC 0031 §D person-keyed amendment (v0.3.7 finding F-3b): a person's
identity is saved as a ``contact:<participant_id>`` note, but the
query-driven notes recall is room-scoped and lexical, so a persona could
not recall *who it is talking to* in a new room. This helper recalls the
event sender's contact note cross-room (identity attaches to the person,
not the venue — ``docs/memory-scope-axes.md``) — still principal/epoch
scoped and topic-exact — and merges it ahead of the room-scoped notes so
it rides the same ``recent_notes`` budget.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..memory.episodic import EpisodicMemory
    from ..memory.notes import Note
    from ..persona_types import AgentEvent

logger = logging.getLogger(__name__)


async def merge_sender_contact_notes(
    episodic: EpisodicMemory,
    event: AgentEvent,
    agent_id: str,
    room_notes: list[Note],
) -> list[Note]:
    """Return ``room_notes`` with the event sender's cross-room contact
    notes prepended (dedup by id).

    Contact-first because "who is this" outranks a stale room note when the
    budget is tight. Returns ``room_notes`` unchanged when the event has no
    sender, the recall raises (non-fatal), or nothing is stored.
    """
    sender_id = getattr(event, "sender_id", None)
    if not sender_id:
        return room_notes
    try:
        # ``limit=3`` deliberately, *not* the API default of 10: this is an
        # auto-injection read on every inbound event, and these notes are
        # prepended ahead of the room-scoped notes' own budget. A person
        # normally has a single ``contact:<id>`` topic, but ``store_note``
        # can append several notes under it — 3 gives headroom for a
        # multi-fact identity while keeping "who is this" from crowding out
        # room context. Keep this small; do not raise it to the API default.
        contact_notes = await episodic.recall_contact_notes(sender_id, limit=3)
    except Exception:
        logger.warning(
            "Agent %s: contact-note recall failed, skipping",
            agent_id, exc_info=True,
        )
        return room_notes
    if not contact_notes:
        return room_notes
    seen = {n.id for n in contact_notes}
    return contact_notes + [n for n in room_notes if n.id not in seen]


async def recall_notes_for_event(
    episodic: EpisodicMemory,
    *,
    query: str,
    event: AgentEvent,
    agent_id: str,
    min_score: float | None,
    limit: int = 5,
) -> list[Note]:
    """Recall the notes tier for one event.

    Two passes, merged: the room-scoped, query-driven recall (the §D
    default — ``recall_notes(sessions=None)``) and, prepended, the event
    sender's cross-room ``contact:<id>`` notes (F-3b). Encapsulates the
    "one tier's failure never blocks the event" guard the same way the
    relationship / channel-history / facts tier helpers do, so
    :meth:`_inject_memory_context` stays a thin orchestrator.
    """
    try:
        room_notes = await episodic.recall_notes(
            query, limit=limit, min_score=min_score, sessions=None,
        )
    except Exception:
        logger.warning(
            "Agent %s: note recall failed, skipping", agent_id, exc_info=True,
        )
        room_notes = []
    return await merge_sender_contact_notes(
        episodic, event, agent_id, room_notes,
    )
