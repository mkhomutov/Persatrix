"""Relationship memory tier — recall and admission for ``_inject_memory_context``.

The relationship tier sits at priority 8 (highest) in the canonical
cross-RFC priority order pinned by RFC 0011 §E and RFC 0021 §J.  Two
helpers:

- :func:`recall_relationship_summary` issues the per-sender
  relationship lookup; swallows backend failures so one tier's
  failure cannot block downstream tiers.

- :func:`render_relationship_section` runs :class:`MemoryBudget`
  admission and builds the ``"relationship_context"``
  :class:`WorkingMemory` section.  Returns ``None`` when nothing is
  admitted so the caller can skip ``add_section``.

Extracted from :mod:`agents.persona_runtime.memory_context` so the
mixin file stays under the 500-line review cap; the tier is logically
independent and parallels :mod:`agents.persona_runtime.channel_history`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from ..memory.working import ContextSection, estimate_tokens
from ..observability.metrics import current_agent_id, try_get_instruments
from ..temporal.rendering import format_cadence, format_relative
from .memory_budget import (
    MIN_TOKENS_RELATIONSHIP,
    REL_NOTES_INTERIM_CHARS,
    MemoryBudget,
)

if TYPE_CHECKING:
    from ..memory.relationship import RelationshipMemory
    from ..memory.relationship_types import RelationshipSummary
    from ..persona_types import AgentEvent

logger = logging.getLogger(__name__)

__all__ = [
    "RELATIONSHIP_SECTION_NAME",
    "RELATIONSHIP_SECTION_PRIORITY",
    "recall_relationship_summary",
    "render_relationship_section",
]


# Section identity exported so the caller's section-clear sweep and
# tests pin against a single name source.
RELATIONSHIP_SECTION_NAME: str = "relationship_context"
RELATIONSHIP_SECTION_PRIORITY: int = 8

# A trust score of exactly _DEFAULT_TRUST_SCORE (the initial value)
# provides no useful signal to the LLM.  Inject trust only when it has
# deviated by more than _TRUST_DEVIATION_THRESHOLD from the default.
# (PR #60 review: unnamed magic numbers in trust comparison.)
_DEFAULT_TRUST_SCORE: float = 0.5
_TRUST_DEVIATION_THRESHOLD: float = 0.01


async def recall_relationship_summary(
    rel_memory: RelationshipMemory,
    event: AgentEvent,
    *,
    agent_id: str,
) -> RelationshipSummary | None:
    """Look up the relationship summary for ``event.sender_id``.

    Returns ``None`` for events without a sender (orchestrator events,
    self-emitted events) and on backend failure (logged at WARNING so
    the rest of the budget pipeline keeps running).  Sender's
    participant type is extracted from ``event.metadata`` so user
    relationships are queried with ``other_participant_type="user"``;
    falls back to ``"agent"`` when metadata is missing.
    (PR #120 review F-1: other_participant_type not propagated.)
    """
    sender_id = event.sender_id
    if not sender_id:
        return None
    # TODO(v0.3): sanitize other_participant_id alongside rel.notes
    # below when A2A allows external agents — the id flows directly
    # into the LLM-visible label and could carry injection content
    # if/when external agents may register arbitrary IDs.
    # (PR #146 re-review: low-risk alignment with rel.notes TODO.)
    sender_type = (
        event.metadata.get("sender_participant_type", "agent")
        if event.metadata
        else "agent"
    )
    try:
        return await rel_memory.get_relationship_summary(
            sender_id,
            other_participant_type=sender_type,
        )
    except Exception:
        logger.warning(
            "Agent %s: relationship lookup for %s failed, skipping",
            agent_id, sender_id, exc_info=True,
        )
        return None


def render_relationship_section(
    rel: RelationshipSummary | None,
    budget: MemoryBudget,
    *,
    now: float,
    timezone: str,
    truncate: Callable[[str, int], str],
) -> ContextSection | None:
    """Build the ``relationship_context`` :class:`WorkingMemory` section.

    Returns ``None`` for empty relationships (no recorded interactions)
    or when the budget admits nothing.

    Increments ``agent.temporal.recency.rendered`` with
    ``source="relationship"`` after a successful admission when the
    rendered content contained a ``Last seen`` line.  Counting on
    admission (not on attempt) matches PR #260 review M-1 so the
    metric reflects what reached the prompt.  Truncation at the
    ``MIN_TOKENS_RELATIONSHIP=64`` floor snips from the end; the
    ``Last seen`` line sits near the front of the rendered block
    (after header, trust, interactions) so it survives the floor for
    any realistic field sizes.
    """
    if rel is None or rel.interaction_count <= 0:
        return None
    if rel.other_participant_type == "user":
        label = f"{rel.other_participant_id} (Human user)"
    else:
        label = rel.other_participant_id
    rel_lines = [f"Relationship with {label}:"]
    # F-60-4: skip default trust injection — a score equal to the
    # initial value carries no useful signal to the LLM.
    if abs(rel.trust_score - _DEFAULT_TRUST_SCORE) > _TRUST_DEVIATION_THRESHOLD:
        rel_lines.append(f"  Trust: {rel.trust_score:.2f}")
    rel_lines.append(f"  Interactions: {rel.interaction_count}")
    rendered_last_seen = False
    if rel.last_interaction_at is not None:
        last_seen = format_relative(rel.last_interaction_at, now, timezone)
        rel_lines.append(f"  Last seen: {last_seen}")
        rendered_last_seen = True
    cadence = format_cadence(
        rel.interaction_count, rel.first_interaction_at,
        rel.last_interaction_at, now,
    )
    if cadence is not None:
        rel_lines.append(f"  Cadence: {cadence}")
    if rel.notes:
        # TODO(v0.3): sanitize rel.notes when A2A protocol allows
        # external agents — a compromised peer could store prompt
        # injection text in its relationship notes.
        # (PR #60 review: internal prompt injection via peer memory.)
        # The per-block budget alone allows ~6000 chars of notes if
        # the relationship tier wins the budget; the per-field char
        # cap keeps the worst-case injection surface bounded
        # independent of budget allocation order.  Routed through the
        # shared word-boundary + ellipsis helper so the LLM-visible
        # truncation marker matches the episodic and notes tiers.
        # (PR #146 review.)
        capped_notes = truncate(rel.notes, REL_NOTES_INTERIM_CHARS)
        rel_lines.append(f"  Notes: {capped_notes}")
    rel_text = "\n".join(rel_lines)
    admitted_rel = budget.try_add(rel_text, min_tokens=MIN_TOKENS_RELATIONSHIP)
    if admitted_rel is None:
        return None
    if rendered_last_seen:
        instruments = try_get_instruments()
        if instruments is not None:
            instruments.temporal_recency_rendered.add(
                1,
                attributes={
                    "agent.id": current_agent_id(),
                    "source": "relationship",
                },
            )
    return ContextSection(
        name=RELATIONSHIP_SECTION_NAME,
        # PR 6 — PR 2 review finding 3: pass ``accurate=True`` so this
        # tier's WorkingMemory token count uses tiktoken, matching the
        # authoritative count produced by ``MemoryBudget`` above.  The
        # two layers agree on the same accounting, so operators
        # debugging "why was my section dropped" do not have to
        # reconcile a chars/4 estimate against a tiktoken-measured
        # budget.
        content=admitted_rel,
        priority=RELATIONSHIP_SECTION_PRIORITY,
        token_count=estimate_tokens(admitted_rel, accurate=True),
        compressible=True,
    )
