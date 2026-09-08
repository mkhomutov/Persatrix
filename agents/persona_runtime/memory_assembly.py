"""Allocate-loop half of the persona memory context injection.

Split out of :mod:`agents.persona_runtime.memory_context` (v0.3.16
Workstream D, ISSUE-0143 PR D1) at the seam between *recall and gate*
and *admit and stage*.  ``memory_context`` reads the tiers, applies the
RFC 0037 §D gate and owns the per-turn :class:`MemoryBudget`; this
module renders each gated tier against that budget in the canonical
priority order and stages the admitted sections in working memory.

The tier recalls and the budget constructor stay behind deliberately:
the test harnesses patch ``recall_room_ranked`` and ``MemoryBudget`` by
``memory_context``'s name, and a moved call would silently escape those
patches.  The channel-roster tier stays behind too — it is injected
outside the budget, and v0.3.16 PR A1 moves it ahead of the gate.

Behaviour is what the mixin inlined before the split, unchanged.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .channel_history import render_channel_history_section
from .episodic_section import render_episodic_section
from .facts_section import render_facts_section
from .notes_section import render_notes_section
from .relationship_section import render_relationship_section

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..memory.episodic import Episode
    from ..memory.facts import Fact
    from ..memory.notes import Note
    from ..memory.relationship_types import RelationshipSummary
    from .memory_budget import MemoryBudget
    from .memory_context import _MemoryContextMixin

logger = logging.getLogger(__name__)

__all__ = ["inject_admitted_sections"]


async def inject_admitted_sections(
    agent: _MemoryContextMixin,
    *,
    budget: MemoryBudget,
    now: float,
    rel: RelationshipSummary | None,
    channel_episodes: list[Episode],
    facts: list[Fact],
    episodes: list[Episode],
    notes: list[Note],
    truncate: Callable[[str, int], str],
) -> None:
    """Render the gated tiers against *budget* and stage them in working memory.

    Tiers are processed in fixed priority order (relationship=8 →
    channel history → facts → episodic=7 → notes=6); higher-priority
    tiers consume the budget first (RFC 0017 §B / OQ4).  Each tier's
    ``render_*`` helper owns its admission, line shape and MQ-11
    provenance; this loop only sequences them and stages the sections.
    The facts tier also issues the RFC 0026 PR 4 use-based reinforcement
    write, non-fatal because the section is already staged.

    *agent* is the :class:`_MemoryContextMixin` instance whose working
    memory, fact store and per-tier settings the loop reads.  *truncate*
    is the mixin's ``_truncate_with_ellipsis`` — passed in rather than
    imported so the ``memory_context → memory_assembly`` import stays
    one-directional.
    """
    working_memory = agent._working_memory

    # Relationship tier (priority 8).  Admission, label formatting,
    # default-trust filtering, and the temporal-recency metric live
    # in ``relationship_section.render_relationship_section``.
    rel_section = render_relationship_section(
        rel, budget,
        now=now, timezone=agent._timezone, truncate=truncate,
    )
    if rel_section is not None:
        working_memory.add_section(rel_section)

    # Channel-history tier (RFC 0011 §E + RFC 0021 §J).  Slots
    # between relationship and episodic admissions.
    ch_section = render_channel_history_section(
        channel_episodes, budget,
        now=now, timezone=agent._timezone, truncate=truncate,
    )
    if ch_section is not None:
        working_memory.add_section(ch_section)

    # Facts tier (RFC 0026 PR 3).  Admitted between channel_history
    # and episodic so a high-signal fact displaces lower-signal
    # prose under budget pressure.  Header charged against the
    # global budget; admitted fact_ids land on the per-turn
    # registry for the PR 4 reinforcement write below (MQ-11).
    if facts:
        facts_section = render_facts_section(
            facts, budget,
            facts_budget_tokens=agent._facts_budget_tokens,
        )
        if facts_section is not None:
            working_memory.add_section(facts_section)
            # RFC 0026 PR 4 — use-based reinforcement; non-fatal
            # because the section is already staged and the caller
            # builds the prompt after this returns (PR #342 M-3).
            admitted_fact_ids = budget.admissions_by_tier("facts")
            if admitted_fact_ids and agent._fact_store is not None:
                try:
                    await agent._fact_store.mark_recalled(admitted_fact_ids)
                except Exception:
                    logger.warning(
                        "Agent %s: fact reinforcement write failed; skipping",
                        agent.agent_id, exc_info=True,
                    )

    # Episodic tier (priority 7).  Extracted to
    # ``episodic_section.render_episodic_section`` (F-4 slice B) so the
    # episodic tier matches the other recall tiers' ``render_*`` shape;
    # behaviour (recency tags, budget admission, MQ-11 provenance,
    # ``source="episode"`` metric) is preserved there.
    ep_section = render_episodic_section(
        episodes, budget,
        now=now, timezone=agent._timezone, truncate=truncate,
    )
    if ep_section is not None:
        working_memory.add_section(ep_section)

    # Notes tier (priority 6).  Extracted to
    # ``notes_section.render_notes_section`` (RFC 0037 PR 4) so the
    # notes tier matches the other tiers' ``render_*`` shape;
    # behaviour (line shape, budget admission, MQ-11 provenance) is
    # preserved there.
    notes_section = render_notes_section(
        notes, budget, truncate=truncate,
    )
    if notes_section is not None:
        working_memory.add_section(notes_section)
