"""Assemble the Tier-C compose system prompt for ``_LLMPersonaAgent``.

Extracted from :mod:`agents.persona_runtime.action_loop` so that file stays
under the 500-line review cap (``scripts/checks/file_size.py``) once the RFC 0051
Phase 5 reflexion call lands beside it — the named next extraction candidate in
the [RFC 0051 PR plan](../../docs/rfcs/0051-pr-plan.md) File-size constraints
table, and the same carve-out idiom that pulled the ingest sanitizer into
``channel_ingest.py`` and the LLM-error dispatch into ``llm_call_errors.py``.

The assembly is a cohesive concern: the persona system prompt, then the RFC 0034
working-memory sections (highest-priority first, budget-trimmed by
``WorkingMemory.build_context``), then — under ``reasoning.mode: plan`` — the
private RFC 0051 :class:`~agents.persona_runtime.deliberation_plan.CompositionPlan`
section. The plan is the persona's own trusted reasoning, a normal system-prompt
section, **never** an ``AgentAction`` and never persisted (the §E privacy wall).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .deliberation_plan import render_plan_section

if TYPE_CHECKING:
    from .salience_gate import SalienceOutcome

__all__ = ["build_compose_system_prompt"]


def build_compose_system_prompt(
    agent: Any, salience: SalienceOutcome | None,
) -> str:
    """Build the compose system prompt: persona base + working memory + plan.

    ``agent`` is the :class:`_LLMPersonaAgent` (passed rather than bound as a
    method to keep ``action_loop.py`` thin); the assembly reads its
    ``_build_system_prompt`` and ``_working_memory``. The plan section is appended
    only when the Tier-B seam carried a parseable plan back (``mode: plan``);
    ``off``/``bid`` and an unparseable plan compose unplanned (RFC 0051 §Phase 2).
    """
    system_prompt = agent._build_system_prompt()

    # Retrieve assembled working memory (episodic, relationship, notes) and append
    # to the system prompt so the LLM sees relevant memories. build_context()
    # returns sections sorted by priority (highest first), dropping those that
    # exceed the token budget. Each element is a dict with "role" (the section
    # name, e.g. "episodic_recall") and "content" (the text to inject); only
    # "content" is used — the "role" labels are WorkingMemory identifiers, NOT LLM
    # conversation roles, and are omitted to avoid confusing the LLM with metadata.
    memory_sections = agent._working_memory.build_context()
    if memory_sections:
        system_prompt += "\n\n" + "\n\n".join(s["content"] for s in memory_sections)

    # RFC 0051 PR 3 — thread the private CompositionPlan into the Tier-C compose
    # (never an AgentAction, never persisted — the §E wall). The plan is None on
    # the off/bid rungs and when it failed to parse, so prod stays unchanged until
    # a channel is promoted to mode: plan.
    if salience is not None and salience.plan is not None:
        system_prompt += "\n\n" + render_plan_section(salience.plan)

    return system_prompt
