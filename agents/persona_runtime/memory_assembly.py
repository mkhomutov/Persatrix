"""Allocate-loop half of the persona memory context injection.

Split out of :mod:`agents.persona_runtime.memory_context` (v0.3.16
Workstream D, ISSUE-0143 PR D1) at the seam between *recall and gate*
and *admit and stage*.  ``memory_context`` reads the tiers, applies the
RFC 0037 §D gate and owns the per-turn :class:`MemoryBudget`; this
module renders each gated tier against that budget in the canonical
priority order and stages the admitted sections in working memory.

The tier recalls and the budget construction stay behind deliberately:
the test harnesses patch ``recall_room_ranked`` and monkeypatch the
``MEMORY_BUDGET_TOKENS`` constant by ``memory_context``'s name, and a
moved call would silently escape those patches.  The channel-roster
tier stays behind too — it is injected outside the budget, and v0.3.16
PR A1 moves it ahead of the gate.

This module is a leaf: it takes the handful of per-agent values it
needs as plain arguments rather than the mixin instance, so it knows
nothing about the mixin's attribute layout and can be exercised without
one.  Behaviour is what the mixin inlined before the split, unchanged,
except that the fact-reinforcement failure warning now logs under this
module's logger.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .channel_history import render_channel_history_section
from .episodic_section import render_episodic_section
from .facts_section import render_facts_section
from .notes_section import render_notes_section
from .relationship_section import render_relationship_section
from .text_truncate import _truncate_with_ellipsis

if TYPE_CHECKING:
    from ..memory.episodic import Episode
    from ..memory.facts import Fact, FactStore
    from ..memory.notes import Note
    from ..memory.relationship_types import RelationshipSummary
    from ..memory.working import WorkingMemory
    from .memory_budget import MemoryBudget

logger = logging.getLogger(__name__)

__all__ = ["inject_admitted_sections"]


async def inject_admitted_sections(
    *,
    working_memory: WorkingMemory,
    agent_id: str,
    timezone: str,
    fact_store: FactStore | None,
    facts_budget_tokens: int,
    budget: MemoryBudget,
    now: float,
    rel: RelationshipSummary | None,
    channel_episodes: list[Episode],
    facts: list[Fact],
    episodes: list[Episode],
    notes: list[Note],
) -> None:
    """Render the gated tiers against *budget* and stage them in *working_memory*.

    Tiers are processed in fixed priority order (relationship=8 →
    channel history → facts → episodic=7 → notes=6); higher-priority
    tiers consume the budget first (RFC 0017 §B / OQ4).  Each tier's
    ``render_*`` helper owns its admission, line shape and MQ-11
    provenance; this loop only sequences them and stages the sections.
    The facts tier also issues the RFC 0026 PR 4 use-based reinforcement
    write against *fact_store*, non-fatal because the section is already
    staged.

    *agent_id*, *timezone* and *facts_budget_tokens* are the mixin's
    per-agent settings, passed as values so this module stays a leaf.
    """
    truncate = _truncate_with_ellipsis

    # Relationship tier (priority 8).  Admission, label formatting,
    # default-trust filtering, and the temporal-recency metric live
    # in ``relationship_section.render_relationship_section``.
    rel_section = render_relationship_section(
        rel, budget, now=now, timezone=timezone, truncate=truncate,
    )
    if rel_section is not None:
        working_memory.add_section(rel_section)

    # Channel-history tier (RFC 0011 §E + RFC 0021 §J).  Slots
    # between relationship and episodic admissions.
    ch_section = render_channel_history_section(
        channel_episodes, budget, now=now, timezone=timezone, truncate=truncate,
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
            facts, budget, facts_budget_tokens=facts_budget_tokens,
        )
        if facts_section is not None:
            working_memory.add_section(facts_section)
            # RFC 0026 PR 4 — use-based reinforcement; non-fatal
            # because the section is already staged and the caller
            # builds the prompt after this returns (PR #342 M-3).
            admitted_fact_ids = budget.admissions_by_tier("facts")
            if admitted_fact_ids and fact_store is not None:
                try:
                    await fact_store.mark_recalled(admitted_fact_ids)
                except Exception:
                    logger.warning(
                        "Agent %s: fact reinforcement write failed; skipping",
                        agent_id, exc_info=True,
                    )

    # Episodic tier (priority 7).  Extracted to
    # ``episodic_section.render_episodic_section`` (F-4 slice B) so the
    # episodic tier matches the other recall tiers' ``render_*`` shape;
    # behaviour (recency tags, budget admission, MQ-11 provenance,
    # ``source="episode"`` metric) is preserved there.
    ep_section = render_episodic_section(
        episodes, budget, now=now, timezone=timezone, truncate=truncate,
    )
    if ep_section is not None:
        working_memory.add_section(ep_section)

    # Notes tier (priority 6).  Extracted to
    # ``notes_section.render_notes_section`` (RFC 0037 PR 4) so the
    # notes tier matches the other tiers' ``render_*`` shape;
    # behaviour (line shape, budget admission, MQ-11 provenance) is
    # preserved there.
    notes_section = render_notes_section(notes, budget, truncate=truncate)
    if notes_section is not None:
        working_memory.add_section(notes_section)
