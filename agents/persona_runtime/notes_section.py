"""Notes-tier recall for the persona memory context.

Factored out of :mod:`agents.persona_runtime.memory_context` to keep that
module under the 500-line code cap and to mirror the other tier helpers
(:mod:`relationship_section` / :mod:`facts_section`).

The notes tier is **room-scoped**: a note is recalled only in the
session/room it was written in (the RFC 0031 §D default —
``recall_notes(sessions=None)``).  Person identity that must follow a
person *across* rooms is no longer stored here — RFC 0031 amendment (F-7
Option D, ISSUE-0093) re-homed it onto the cross-room **relationship**
tier (:mod:`relationship_section`), retiring the earlier ``contact:*``
cross-room note carve-out (PR D3).  This helper therefore does the single
room-scoped query recall and nothing more.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..memory.episodic import EpisodicMemory
    from ..memory.notes import Note
    from ..persona_types import AgentEvent

logger = logging.getLogger(__name__)


async def recall_notes_for_event(
    episodic: EpisodicMemory,
    *,
    query: str,
    event: AgentEvent,  # noqa: ARG001 — kept for tier-helper signature parity
    agent_id: str,
    min_score: float | None,
    limit: int = 5,
) -> list[Note]:
    """Recall the notes tier for one event (room-scoped, query-driven).

    The RFC 0031 §D default (``recall_notes(sessions=None)``) — notes are
    recalled from the active room plus the ``legacy`` carve-out, never
    cross-room.  Encapsulates the "one tier's failure never blocks the
    event" guard the same way the relationship / channel-history / facts
    tier helpers do, so :meth:`_inject_memory_context` stays a thin
    orchestrator.

    ``event`` is accepted for signature parity with the other tier helpers
    (and so a future per-sender notes signal can be added without churning
    the call site); it is unused now that cross-room contact recall lives
    on the relationship tier.
    """
    try:
        return await episodic.recall_notes(
            query, limit=limit, min_score=min_score, sessions=None,
        )
    except Exception:
        logger.warning(
            "Agent %s: note recall failed, skipping", agent_id, exc_info=True,
        )
        return []
