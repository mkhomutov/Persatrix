"""Declarative-fact memory tier — recall and admission for ``_inject_memory_context``.

RFC 0026 PR 3 — the facts tier slots between the relationship and
notes tiers in the canonical cross-RFC priority order (RFC 0027 §F
end-state).  Two helpers:

- :func:`recall_facts_for_event` issues
  :meth:`agents.memory.facts.FactStore.recall` for each subject derived
  from *event* (today: the canonicalised ``sender_id``; later RFCs will
  add mentioned entities to the seed set).  Returns ``[]`` for events
  with no resolvable subject, for missing / mis-initialised
  ``FactStore``, and on backend failure (the latter is logged at
  WARNING so the rest of the budget pipeline keeps running).

- :func:`render_facts_section` runs :class:`MemoryBudget` admission and
  builds the ``"facts_context"`` :class:`WorkingMemory` section.  The
  per-tier slice is bounded by ``facts_budget_tokens`` (default 200,
  per RFC 0026 OQ #2).  The header is charged against the budget
  inside the ``if items:`` block so the RFC 0017 PR 2 finding #2
  under-report bug does not recur — see :ref:`PR plan §PR 3 key
  details <facts-recall-budget>`.

The facts tier is the load-bearing fix for MT-MEMORY-005 Legs 1 / 2 / 5
(the dementia-test core): a fact stored at interaction N is injected at
N+1 even when the follow-up query does not mention the subject string,
because admission keys on the canonical sender, not on text overlap.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..memory.fact_predicates import canonicalize_subject
from ..memory.working import ContextSection, estimate_tokens
from ..observability.metrics import current_agent_id, try_get_instruments
from .memory_budget import MemoryBudget

if TYPE_CHECKING:
    from ..memory.facts import Fact, FactStore
    from ..persona_types import AgentEvent

logger = logging.getLogger(__name__)

__all__ = [
    "FACTS_SECTION_NAME",
    "FACTS_SECTION_PRIORITY",
    "MIN_TOKENS_FACTS",
    "DEFAULT_FACTS_BUDGET_TOKENS",
    "FACTS_RECALL_LIMIT",
    "recall_facts_for_event",
    "render_facts_section",
    "resolve_facts_config",
]


def resolve_facts_config(config: dict) -> tuple[bool, int]:
    """Resolve ``memory.facts.{enabled,budget_tokens}`` from a persona config.

    Centralised here so :class:`_LLMPersonaAgent.__init__` does not
    have to import the default constants directly and so the schema
    layer has a single place to look up Phase-1 defaults.
    """
    facts_cfg = (config.get("memory") or {}).get("facts") or {}
    enabled = bool(facts_cfg.get("enabled", True))
    budget_tokens = int(
        facts_cfg.get("budget_tokens", DEFAULT_FACTS_BUDGET_TOKENS),
    )
    return enabled, budget_tokens


# Section identity exported so the caller's section-clear sweep and
# tests pin against a single name source.
FACTS_SECTION_NAME: str = "facts_context"
# Priority 7 places the section above notes (6) and below relationship
# (8) / channel_history (7) — the canonical order pinned by RFC 0027 §F.
# The budget allocation order is governed by the call sequence, not by
# this number; this attribute drives the WorkingMemory render order.
FACTS_SECTION_PRIORITY: int = 7

# Per-item floor for ``MemoryBudget.try_add``.  Facts are 1-2 lines so
# the floor is closer to ``MIN_TOKENS_NOTES=24`` than to
# ``MIN_TOKENS_RELATIONSHIP=64``.  A truncated fact line shorter than
# this would lose either the predicate or the object — both load-bearing
# for the dementia-test invariant.
MIN_TOKENS_FACTS: int = 24

# Default per-tier soft slice (RFC 0026 OQ #2 — ≈13% of the RFC 0017
# 1500-token allocator).  Operators override via
# ``memory.facts.budget_tokens`` in :ref:`config/agents.yaml
# <agents-yaml>`.  Soft, not hard: the global allocator still owns the
# 1500-token ceiling; this constant decides how much of that ceiling
# the facts tier may consume on any single event.
DEFAULT_FACTS_BUDGET_TOKENS: int = 200

# Maximum number of fact rows pulled from :meth:`FactStore.recall` per
# subject.  The budget allocator decides which fit; this cap is the
# row-count floor (mirrors ``CHANNEL_RECALL_LIMIT=20``).  Bounded so a
# pathological subject with hundreds of live facts cannot blow up the
# allocator's per-item token-count loop.
FACTS_RECALL_LIMIT: int = 20


def _subject_seeds(event: AgentEvent) -> list[str]:
    """Derive the canonical subject set for ``event``.

    v0.3.1 ships the sender-only seed.  Future RFCs (mentioned-entity
    extraction, multi-subject queries) extend this list; PR 3 keeps the
    seam narrow so the unit-test surface stays predictable.

    Returns canonicalised subjects (post :func:`canonicalize_subject`).
    A ``None`` / empty / whitespace-only sender yields an empty list
    so the tier short-circuits at the caller.
    """
    sender_id = event.sender_id
    if not sender_id or not sender_id.strip():
        return []
    try:
        canonical = canonicalize_subject(sender_id)
    except ValueError:
        # Defensive: sender_id should already be non-empty per the
        # truthiness check above, but if a caller injects a payload
        # that canonicalises to empty (e.g. a string of only Unicode
        # whitespace), drop the seed rather than raising into the
        # allocator path.
        return []
    return [canonical]


async def recall_facts_for_event(
    fact_store: FactStore | None,
    event: AgentEvent,
    *,
    agent_id: str,
    limit: int = FACTS_RECALL_LIMIT,
) -> list[Fact]:
    """Recall declarative facts for every subject derived from *event*.

    Returns ``[]`` for:

    * ``fact_store is None`` (operator disabled the tier or a test
      harness omitted it),
    * events without a resolvable sender (TICK, orchestrator events),
    * recall backend failures — logged at WARNING so the rest of the
      budget pipeline keeps running (parallels the relationship tier's
      log-and-continue idiom).

    Each subject is recalled independently; duplicate ``fact_id`` rows
    are de-duplicated in caller order so a fact about the sender that
    is also about a mentioned entity (future RFC) is rendered once.
    """
    if fact_store is None:
        return []
    seeds = _subject_seeds(event)
    if not seeds:
        return []
    collected: list[Fact] = []
    seen_ids: set[str] = set()
    for subject in seeds:
        try:
            rows = await fact_store.recall(subject=subject, limit=limit)
        except Exception:
            logger.warning(
                "Agent %s: facts recall for subject=%r failed; skipping",
                agent_id, subject, exc_info=True,
            )
            continue
        for fact in rows:
            if fact.fact_id in seen_ids:
                continue
            seen_ids.add(fact.fact_id)
            collected.append(fact)
    return collected


def _format_fact_line(fact: Fact) -> str:
    """One-line ``- subject predicate object`` render.

    The predicate is intentionally rendered as the raw verb (no
    prettification): the LLM gets the same shape the extractor wrote,
    and operators reading prompt dumps can grep for the canonical
    vocabulary without a translation layer.
    """
    return f"- {fact.subject} {fact.predicate} {fact.object}"


def render_facts_section(
    facts: list[Fact],
    budget: MemoryBudget,
    *,
    facts_budget_tokens: int = DEFAULT_FACTS_BUDGET_TOKENS,
) -> ContextSection | None:
    """Build the ``facts_context`` :class:`WorkingMemory` section.

    Calls :meth:`MemoryBudget.try_add` for each fact line and returns
    the constructed :class:`ContextSection`, or ``None`` when no item
    is admitted.  The per-tier slice ``facts_budget_tokens`` is a
    soft cap on the tier's consumption of the shared 1500-token
    allocator — once admitted token count reaches the slice the loop
    stops, leaving the remainder of the budget for the notes /
    episodic tiers downstream.

    Header accounting (RFC 0017 PR 2 finding #2 regression guard)
    -------------------------------------------------------------
    The ``"Known facts about <subject>:\\n"`` header is charged against
    the budget inside the ``if items:`` block via
    :meth:`MemoryBudget.try_add` so ``memory_admitted_tokens`` does
    not under-report the prompt-side cost.  If the header itself
    cannot be admitted (every other tier already saturated the
    budget), the section is dropped — the per-item lines without a
    framing header are useless to the LLM.

    Subject-templated header (PR #341 review M-2)
    ---------------------------------------------
    The header names the canonical subject of the facts rather than
    addressing the LLM persona as ``"you"``.  Reason: PR 3 admits
    facts about the counterparty (the canonical ``event.sender_id``),
    so a literal ``"Known facts about you:"`` invites the persona to
    interpret a row like ``- bob has_child_named Mira`` as a fact
    about *itself* — the persona-inversion footgun that the dementia
    test is meant to fence off.  Phase 1 invariant: every admitted
    fact shares one subject because :func:`_subject_seeds` yields a
    single seed (the canonical sender); the subject is read off
    ``facts[0].subject`` (the storage form, identical across the
    list).  When PR 4 lands multi-subject seeding the section shape
    will need to fan out (one block per subject, or a pluralised
    header) — tracked in :doc:`docs/rfcs/0026-pr-plan.md` PR 4 scope.

    Telemetry
    ---------
    Each admitted fact increments ``agent.facts.injected`` with
    ``agent.id`` attribute so operators can correlate dementia-test
    leg pass-rates against actual injection volume per agent.
    Counting on admission (not on attempt) matches PR #260 review
    M-1 so the counter reflects what reached the prompt, not what the
    recall layer returned.
    """
    if not facts:
        return None

    instruments = try_get_instruments()
    agent_attr = current_agent_id()

    items: list[str] = []
    facts_tokens_used = 0
    for fact in facts:
        if facts_tokens_used >= facts_budget_tokens:
            break
        if budget.remaining <= 0:
            break
        remaining_before = budget.remaining
        line = _format_fact_line(fact)
        admitted = budget.try_add(line, min_tokens=MIN_TOKENS_FACTS)
        if admitted is None:
            continue
        items.append(admitted)
        facts_tokens_used += remaining_before - budget.remaining
        if instruments is not None:
            instruments.facts_injected.add(
                1, attributes={"agent.id": agent_attr, "tier": "facts"},
            )

    if not items:
        return None

    # Phase 1 invariant: every fact in ``facts`` shares one subject (the
    # canonical sender from :func:`_subject_seeds`); ``facts[0].subject``
    # is the storage form (casefold + whitespace-folded by
    # :func:`canonicalize_subject`), so the header tracks the row's
    # join key rather than the raw ``event.sender_id`` casing — see
    # the "Subject-templated header" section of the function docstring.
    subject = facts[0].subject
    header = f"Known facts about {subject}:\n"
    # Charge the header against the global budget so admitted-token
    # accounting matches the actual prompt-side cost.  If the header
    # cannot be admitted at the per-tier floor, the section would
    # render naked lines without a framing — drop instead.
    admitted_header = budget.try_add(header, min_tokens=MIN_TOKENS_FACTS)
    if admitted_header is None:
        return None

    text = admitted_header + "\n".join(items)
    return ContextSection(
        name=FACTS_SECTION_NAME,
        content=text,
        priority=FACTS_SECTION_PRIORITY,
        token_count=estimate_tokens(text, accurate=True),
        compressible=True,
    )
