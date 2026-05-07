"""Channel-history memory tier (RFC 0011 §E + RFC 0021 §J).

The tier slots between the relationship and episodic tiers in the
canonical cross-RFC priority order pinned by RFC 0011 §E and RFC 0021
§J.  Open commitments and duration priors are deferred to v0.4.0; in
v0.3.0 those slots ship empty.

Two helpers:

- :func:`recall_channel_episodes` issues the per-channel recall.  It is
  a no-op for non-``CHANNEL_MESSAGE`` events and returns an empty list
  on under-populated events (no channel id / no DM peer).  Empty query
  string routes recall through ``recall_recency`` because FTS5's
  implicit-AND default would silently drop most natural-language
  queries against any single channel-scope episode regardless of
  relevance.  RFC 0011 §E framing ("recent channel turns") makes the
  scope filter the precision mechanism, not text overlap.

- :func:`render_channel_history_section` runs the
  :class:`MemoryBudget` admission loop for the recalled set and builds
  the ``"channel_history"`` :class:`WorkingMemory` section.  Returns
  ``None`` when nothing is admitted so the caller can skip the
  ``add_section`` call.

Extracted from :mod:`agents.persona_runtime.memory_context` so that
file stays under the 500-line review cap; the tier is logically
independent and testable in isolation.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from ..memory.scope_recall import recall_with_scope_filter
from ..memory.scopes import scope_for_channel_event
from ..memory.working import ContextSection, estimate_tokens
from ..persona_types import EventType
from ..temporal.rendering import format_duration, format_relative
from .memory_budget import (
    CHANNEL_RECALL_LIMIT,
    MAX_EPISODE_SUMMARY_CHARS,
    MIN_TOKENS_CHANNEL_HISTORY,
    MemoryBudget,
)

if TYPE_CHECKING:
    from ..memory.episodic import EpisodicMemory
    from ..memory.episodic_queries import Episode
    from ..persona_types import AgentEvent

logger = logging.getLogger(__name__)

__all__ = [
    "CHANNEL_HISTORY_SECTION_NAME",
    "CHANNEL_HISTORY_SECTION_PRIORITY",
    "recall_channel_episodes",
    "render_channel_history_section",
]


# Section identity exported so the caller's section-clear sweep and
# tests can pin against a single name source.
CHANNEL_HISTORY_SECTION_NAME: str = "channel_history"
# Priority 7 places the section between relationship (8) and the
# remaining tiers in the working-memory render order.  The budget
# allocation order is determined by the call sequence, not this number.
CHANNEL_HISTORY_SECTION_PRIORITY: int = 7


async def recall_channel_episodes(
    episodic: EpisodicMemory,
    event: AgentEvent,
    *,
    agent_id: str,
) -> list[Episode]:
    """Recall same-channel-scope episodes for *event* (RFC 0011 §E).

    Returns ``[]`` for non-``CHANNEL_MESSAGE`` events and for
    ``CHANNEL_MESSAGE`` events whose scope cannot be resolved
    (under-populated payloads, missing DM peer).  Recall failure is
    logged at WARNING and swallowed so the rest of the budget pipeline
    keeps running.
    """
    if event.event_type is not EventType.CHANNEL_MESSAGE:
        return []
    payload = event.payload or {}
    channel_scope = scope_for_channel_event(
        agent_id,
        channel_id=event.channel_id,
        sender_id=event.sender_id,
        thread_id=event.thread_id,
        channel_type=payload.get("channel_type"),
        # Ingest path already logs scope drift in
        # ``_StatePersistenceMixin._scope_for_multi_turn_event``; recall
        # does not need to emit a second log line.
        on_unknown=None,
    )
    if channel_scope is None:
        return []
    try:
        return await recall_with_scope_filter(
            episodic,
            "",
            limit=CHANNEL_RECALL_LIMIT,
            scope=channel_scope,
        )
    except Exception:
        logger.warning(
            "Agent %s: channel-history recall failed for scope=%s; skipping",
            agent_id, channel_scope, exc_info=True,
        )
        return []


def render_channel_history_section(
    channel_episodes: list[Episode],
    budget: MemoryBudget,
    *,
    now: float,
    timezone: str,
    truncate: Callable[[str, int], str],
) -> ContextSection | None:
    """Build the ``channel_history`` :class:`WorkingMemory` section.

    Calls :meth:`MemoryBudget.try_add` for each channel episode and
    returns the constructed :class:`ContextSection`, or ``None`` if the
    budget admitted nothing.  Renders each line in the same
    ``[recency-tag] summary`` shape as the episodic tier so the
    LLM-visible format stays consistent across tiers.
    """
    if not channel_episodes:
        return None
    items: list[str] = []
    for ep in channel_episodes:
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
        admitted = budget.try_add(
            f"- {prefix} {summary}", min_tokens=MIN_TOKENS_CHANNEL_HISTORY,
        )
        if admitted is not None:
            items.append(admitted)
    if not items:
        return None
    text = "Recent channel turns:\n" + "\n".join(items)
    return ContextSection(
        name=CHANNEL_HISTORY_SECTION_NAME,
        content=text,
        priority=CHANNEL_HISTORY_SECTION_PRIORITY,
        token_count=estimate_tokens(text, accurate=True),
        compressible=True,
    )
