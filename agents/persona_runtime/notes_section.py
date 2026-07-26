"""Notes-tier recall + rendering for the persona memory context.

Factored out of :mod:`agents.persona_runtime.memory_context` to keep that
module under the 500-line code cap and to mirror the other tier helpers
(:mod:`relationship_section` / :mod:`facts_section` /
:mod:`episodic_section` — the render half joined in RFC 0037 PR 4, same
``render_*`` extraction precedent as episodic F-4 slice B).

The notes tier is **room-scoped**: a note is recalled only in the
session/room it was written in (the RFC 0031 §D default —
``recall_notes(sessions=None)``).  Person identity that must follow a
person *across* rooms is no longer stored here — RFC 0031 amendment (F-7
Option D, ISSUE-0093) re-homed it onto the cross-room **relationship**
tier (:mod:`relationship_section`), retiring the earlier ``contact:*``
cross-room note carve-out (PR D3).

RFC 0037 §D (PR 4): the recall is **classification-gated at the query**
— the acting level resolves through the positive-list event rule
(:func:`agents.persona_runtime.injection_gate
.acting_classification_for_event`) to an injectable-level IN-list that
the notes SQL applies non-optionally on this surface, so an above-``L``
note neither surfaces nor consumes a recall-``limit`` slot.  The
Python-side gate in ``_inject_memory_context`` re-checks the returned
rows with every other tier (belt-and-braces; it is where the rule-(c)
aggregated WARNING lives).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..memory.working import ContextSection, estimate_tokens
from .classification import injectable_levels
from .injection_gate import acting_classification_for_event
from .memory_budget import (
    MAX_NOTE_CONTENT_CHARS,
    MIN_TOKENS_NOTES,
    MemoryBudget,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..memory.episodic import EpisodicMemory
    from ..memory.notes import Note
    from ..persona_types import AgentEvent

logger = logging.getLogger(__name__)

#: Working-memory section name + priority for the notes tier.  Pinned here
#: (was an inline literal in ``memory_context``) so the section name and
#: the stale-section clear cannot drift.
NOTES_SECTION_NAME = "recent_notes"
NOTES_SECTION_PRIORITY = 6


async def recall_notes_for_event(
    episodic: EpisodicMemory,
    *,
    query: str,
    event: AgentEvent,
    agent_id: str,
    min_score: float | None,
    limit: int = 5,
) -> list[Note]:
    """Recall the notes tier for one event (room-scoped, query-driven,
    classification-gated).

    The RFC 0031 §D default (``recall_notes(sessions=None)``) — notes are
    recalled from the active room plus the ``legacy`` carve-out, never
    cross-room.  The RFC 0037 §D allowlist is resolved from the trusted
    event (rule (a)/(b) via the positive-list class rule) and passed as
    the non-optional query predicate for this persona read surface.
    Encapsulates the "one tier's failure never blocks the event" guard
    the same way the relationship / channel-history / facts tier helpers
    do, so :meth:`_inject_memory_context` stays a thin orchestrator.
    """
    try:
        return await episodic.recall_notes(
            query, limit=limit, min_score=min_score, sessions=None,
            allowed_protection_levels=injectable_levels(
                acting_classification_for_event(event),
            ),
        )
    except Exception:
        logger.warning(
            "Agent %s: note recall failed, skipping", agent_id, exc_info=True,
        )
        return []


def render_notes_section(
    notes: list[Note],
    budget: MemoryBudget,
    *,
    truncate: Callable[[str, int], str],
) -> ContextSection | None:
    """Build the ``recent_notes`` :class:`WorkingMemory` section.

    Moved verbatim from the inline block in ``_inject_memory_context``
    (RFC 0037 PR 4) so the notes tier matches the other tiers'
    ``render_*`` shape: same ``- [topic] content`` line shape, the same
    budget admission, and the same ``record_admission(tier="notes")``
    MQ-11 provenance (which the §D injection manifest now also reads).
    Returns ``None`` when the budget admits nothing.
    """
    if not notes:
        return None
    note_items: list[str] = []
    for note in notes:
        content = truncate(note.content, MAX_NOTE_CONTENT_CHARS)
        remaining_before = budget.remaining
        admitted = budget.try_add(
            f"- [{note.topic}] {content}",
            min_tokens=MIN_TOKENS_NOTES,
        )
        if admitted is not None:
            note_items.append(admitted)
            # RFC 0026 PR 4 / MQ-11 — uniform per-tier provenance.
            budget.record_admission(
                tier="notes", item_id=note.id,
                tokens_admitted=remaining_before - budget.remaining,
            )
    if not note_items:
        return None
    # Header is added after the loop and is not itself charged against
    # the budget — same accepted under-count as the other tiers.
    text = "Relevant notes:\n" + "\n".join(note_items)
    return ContextSection(
        name=NOTES_SECTION_NAME,
        content=text,
        priority=NOTES_SECTION_PRIORITY,
        token_count=estimate_tokens(text, accurate=True),
        compressible=True,
    )
