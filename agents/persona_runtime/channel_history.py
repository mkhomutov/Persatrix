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
from ..observability.metrics import current_agent_id, try_get_instruments
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

    Recency-fence semantics (PR #264 review M-3): the empty query
    string routes recall through ``EpisodicMemory.recall_recency``,
    which returns the *agent-wide* ``CHANNEL_RECALL_LIMIT * 3 = 60``
    most-recent episodes; the Python-side scope filter in
    ``recall_with_scope_filter`` then narrows to *channel_scope*. This
    is "recent agent activity, restricted to this channel" — **not**
    "the 20 most-recent episodes in this channel". An agent active in
    many channels can have its recency window dominated by other
    channels and admit fewer than ``CHANNEL_RECALL_LIMIT`` entries
    even when more matching channel episodes exist further back.
    Behaviour matches the RFC 0011 §E framing ("recent channel
    turns") but is bounded by the agent-wide recency window.
    Tightening to per-channel recency requires SQL-side scope
    filtering on the recency path (see the TODO in
    ``agents/memory/scope_recall.py``).

    TODO (RFC 0011 PR 6 — channel catch-up fetch): ``on_unknown=None``
    is justified because the in-band ingest path already logs scope
    drift via ``_StatePersistenceMixin._scope_for_multi_turn_event``.
    A future catch-up/replay code path that calls
    ``recall_channel_episodes`` *without* going through ingest first
    would silently fall back to a thread-shaped scope on contradictory
    ``channel_type`` payloads, with no log line surfacing the drift.
    When the catch-up sub-PR lands, either route it through the same
    ingest path or wire a real ``on_unknown`` callback here.
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
        # does not need to emit a second log line.  See the TODO in this
        # function's docstring for the catch-up-fetch follow-up risk.
        on_unknown=None,
    )
    if channel_scope is None:
        return []
    try:
        # RFC 0031 Phase 2 PR 4: explicit ``sessions=None`` documents
        # that channel-history recall takes §D's active-session-plus-
        # legacy default.  The source-level pin in
        # ``test_session_recall_default_path.py`` asserts the all-
        # sessions ``"*"`` sentinel is never reachable from this site.
        return await recall_with_scope_filter(
            episodic,
            "",
            limit=CHANNEL_RECALL_LIMIT,
            scope=channel_scope,
            sessions=None,
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

    Per-admitted-item, increments
    ``agent.temporal.recency.rendered`` with
    ``source="channel_history"`` (PR #264 review M-2).  Same shape and
    rationale as the episodic and relationship tiers: the counter
    description ("Recency tags rendered onto recalled episodes…")
    requires a render-side bump because the channel-history tier emits
    the same ``[recency-tag] summary`` prefix.  Counting on admission
    (not on attempt) matches the PR #260 review M-1 contract pinned by
    ``tests/integration/test_temporal_metrics.py`` so operators see a
    consistent number across all three sources.
    """
    if not channel_episodes:
        return None
    # Resolve OTel surfaces once per call rather than per-item.  Both
    # helpers are cheap (a contextvar read and a module-global lookup),
    # but the loop runs up to CHANNEL_RECALL_LIMIT=20 times.
    instruments = try_get_instruments()
    agent_attr = current_agent_id()
    items: list[str] = []
    for ep in channel_episodes:
        # PR #264 review L4: short-circuit once the budget is
        # exhausted.  ``try_add`` itself returns ``None`` cheaply, but
        # the surrounding work — ``truncate`` over a 280-char summary,
        # ``format_relative`` / ``format_duration`` — is wasted for the
        # tail of the recall set when the budget filled up early.
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
        admitted = budget.try_add(
            f"- {prefix} {summary}", min_tokens=MIN_TOKENS_CHANNEL_HISTORY,
        )
        if admitted is not None:
            items.append(admitted)
            # PR #264 review M-2: bump per admitted item so operators
            # can correlate ``agent.temporal.recency.rendered`` (with
            # ``source="channel_history"``) against admitted-token
            # totals without a phantom shortfall.
            if instruments is not None:
                instruments.temporal_recency_rendered.add(
                    1,
                    attributes={"agent.id": agent_attr, "source": "channel_history"},
                )
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
