"""Episodic-recall tier rendering for the persona memory context.

Extracted from the inline block in ``_inject_memory_context`` (v0.3.7 F-4
slice B) so the episodic tier matches the other recall tiers
(``relationship_section`` / ``channel_history`` / ``facts_section``),
which are all ``render_*`` helpers, and to keep ``memory_context.py``
under the 500-line code cap. Behaviour-preserving: same ``[recency-tag]
summary`` line shape, same budget admission, the same
``record_admission(tier="episodic")`` MQ-11 provenance, and the same
``temporal.recency.rendered`` metric (``source="episode"``) as the inline
block it replaces. Closely mirrors
:func:`channel_history.render_channel_history_section`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from ..memory.working import ContextSection, estimate_tokens
from ..observability.metrics import current_agent_id, try_get_instruments
from ..temporal.rendering import format_duration, format_relative
from .memory_budget import (
    MAX_EPISODE_SUMMARY_CHARS,
    MIN_TOKENS_EPISODIC,
    MemoryBudget,
)

if TYPE_CHECKING:
    from ..memory.episodic_queries import Episode

#: Working-memory section name + priority for the episodic-recall tier.
#: Pinned here (was an inline literal) so the section name and the
#: stale-section clear in ``_inject_memory_context`` cannot drift.
EPISODIC_SECTION_NAME = "episodic_recall"
EPISODIC_SECTION_PRIORITY = 7


def render_episodic_section(
    episodes: list[Episode],
    budget: MemoryBudget,
    *,
    now: float,
    timezone: str,
    truncate: Callable[[str, int], str],
) -> ContextSection | None:
    """Build the ``episodic_recall`` :class:`WorkingMemory` section.

    Calls :meth:`MemoryBudget.try_add` per episode and returns the section
    (or ``None`` if the budget admitted nothing). Each admitted episode is
    recorded on the per-turn provenance registry under the ``episodic``
    tier (MQ-11 / RFC 0026 PR 4) and bumps
    ``agent.temporal.recency.rendered`` with ``source="episode"``.
    """
    if not episodes:
        return None
    instruments = try_get_instruments()
    agent_attr = current_agent_id()
    items: list[str] = []
    for ep in episodes:
        if budget.remaining <= 0:
            break
        summary = truncate(ep.summary, MAX_EPISODE_SUMMARY_CHARS)
        # RFC 0021 §D recency-tag (+ duration on multi-turn rows) prefix.
        anchor_ts = ep.closed_at if ep.closed_at is not None else ep.created_at
        tag = format_relative(anchor_ts, now, timezone)
        if (
            ep.turn_count is not None and ep.turn_count > 1
            and ep.started_at is not None and ep.closed_at is not None
        ):
            dur = format_duration(max(0.0, ep.closed_at - ep.started_at))
            prefix = f"[{tag}, {dur}]"
        else:
            prefix = f"[{tag}]"
        remaining_before = budget.remaining
        admitted = budget.try_add(
            f"- {prefix} {summary}", min_tokens=MIN_TOKENS_EPISODIC,
        )
        if admitted is not None:
            items.append(admitted)
            # MQ-11 — uniform per-tier provenance for the PR 4 reinforcement.
            budget.record_admission(
                tier="episodic", item_id=ep.id,
                tokens_admitted=remaining_before - budget.remaining,
            )
            # PR #260 review M-1: count one per admitted item, not per
            # recalled item — the budget may drop the tail.
            if instruments is not None:
                instruments.temporal_recency_rendered.add(
                    1, attributes={"agent.id": agent_attr, "source": "episode"},
                )
    if not items:
        return None
    # Header is added after the loop and is not itself charged against the
    # budget (~5 tokens) — same accepted under-count as the other tiers.
    text = "Relevant past episodes:\n" + "\n".join(items)
    return ContextSection(
        name=EPISODIC_SECTION_NAME,
        content=text,
        priority=EPISODIC_SECTION_PRIORITY,
        token_count=estimate_tokens(text, accurate=True),
        compressible=True,
    )
