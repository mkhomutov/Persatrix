"""RFC 0034 Phase 1 PR 2 — persona conversation-window substrate.

The persona runtime calls the LLM with a ``messages`` array rebuilt from
scratch every turn that holds **only the current user message** — there
is no short-term conversational memory in the model's context, so
mid-conversation the persona treats every turn as the first turn
(ISSUE-0052).

This module owns the fix: :func:`build_conversation_messages`
reconstructs the ``messages`` array from the channel store on every
persona turn. It fetches the last N messages of ``event.channel_id``,
maps peer messages to ``role="user"`` and the persona's own messages to
``role="assistant"``, sanitizes each replayed peer turn through the same
``<|user_message|>`` delimiter escape the current event already gets,
and appends the current event last.

PR 2 ships the substrate **only** — no call site is wired. RFC 0034
Phase 1 PR 3 installs the call in ``action_loop.py`` immediately before
the ``messages = [...]`` LLM seed.

Design anchors (see ``docs/rfcs/0034-persona-conversational-working-memory.md``):

* **§B — window definition.** The window is the last ``max_turns``
  replayed messages of ``event.channel_id``, per-channel (no session
  filter — see OQ #1 below). The current event is excluded from the
  fetched window (dedup by ``id``) and appended last by this module.
  The fetch over-fetches by one (``max_turns + 1``): the inbound event
  may already be persisted, so dropping its own row would otherwise
  shrink the replayed window to ``max_turns - 1``.
* **§C — role mapping.** ``sender_id == agent_id`` ⇒ ``assistant``; any
  other sender ⇒ ``user``. Phase 1 is DM-only (exactly one peer).
* **§D — sanitization.** Replayed peer turns are formatted through
  ``_PromptAssemblyMixin._format_event``'s ``CHANNEL_MESSAGE`` branch so
  the ``"<|" -> "\\<|"`` / ``"|>" -> "\\|>"`` escape is inherited by
  construction. The escape is never duplicated here.
* **§E — token budget.** ``max_tokens`` bounds the replayed transcript
  separately from the RFC 0017 system-prompt memory budget. Per-turn
  admission applies token-overflow FIFO first, then count-overflow FIFO
  (OQ #2 resolution 2a — tighter bound wins).
* **§F — caching.** An in-process cache keyed by ``(channel_id,
  message_id)`` skips the network fetch when the same event is seen
  twice (retries, sub-agent return paths). Steady-state turn-over-turn
  hit rate is low *by design* — the cache key advances with every
  inbound message; Phase 3 telemetry is the arbiter for any re-spec
  (RFC §F "Known gap" framing (a)). On any fetch failure the window
  degrades to current-event-only — the persona is no worse off than it
  is today.

OQ #1 resolution 1a (per-channel, no session filter): the window
filters on ``event.channel_id`` only; rows are admitted regardless of
``chat_session_id`` / ``persatrix_session_id``. Restarting the chat CLI
under a fresh session id preserves in-channel transcript continuity.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from ..memory.working import estimate_tokens
from ..persona_types import AgentEvent, EventType
from .prompt_assembly import _PromptAssemblyMixin

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..channel_history_fetcher import ChannelHistoryFetcher

logger = logging.getLogger(__name__)

__all__ = [
    "ConversationWindowConfig",
    "build_conversation_messages",
]

# Committed Phase 1 defaults (RFC 0034 OQ #2). A retune is a one-line
# change here once Phase 3 telemetry lands — no schema implication.
DEFAULT_MAX_TURNS: int = 20
DEFAULT_MAX_TOKENS: int = 2048


@dataclass(frozen=True, slots=True)
class ConversationWindowConfig:
    """Per-agent tuning for the conversation window.

    Defaults mirror the top-level ``conversation_window`` block in
    ``config/optimization.yaml``; a per-agent ``conversation_window``
    block in ``config/agents.yaml`` overrides them. ``enabled: false`` is
    the operator escape hatch — :func:`build_conversation_messages` then
    seeds the array with the current event only (pre-RFC-0034
    behaviour). Config resolution into this dataclass is wired by PR 3.
    """

    max_turns: int = DEFAULT_MAX_TURNS
    max_tokens: int = DEFAULT_MAX_TOKENS
    enabled: bool = True


# ─── In-process fetch cache (RFC §F) ───────────────────────
#
# Maps ``channel_id`` to the last ``(message_id, raw_fetched_rows)`` seen
# for that channel. A call whose ``event.message_id`` matches the cached
# id skips the network fetch and re-uses the raw rows. One entry per
# channel — a newer ``message_id`` overwrites it (the "cheapest possible"
# invalidation RFC §F specifies), so the cache is bounded by channel
# count. Raw rows are stored *pre*-role-mapping, so role mapping
# (`sender_id == agent_id`) is re-applied per call and the cache is
# agent-independent.
#
# Phase 2 caveat: the cached rows carry the *first* caller's
# ``max_turns + 1`` fetch limit. A DM channel has exactly one persona, so
# in Phase 1 the same ``(channel_id, message_id)`` is only ever processed
# under one config and that limit is constant. Once a channel can host
# multiple personas with independent ``conversation_window`` configs
# (RFC 0034 Phase 2 — group channels), a small-``max_turns`` persona
# could serve an undersized window to a large-``max_turns`` peer; Phase 2
# must key the cache on the fetch limit or bypass it for such channels.
_WINDOW_CACHE: dict[str, tuple[str, list[dict[str, Any]]]] = {}


# ``_PromptAssemblyMixin._format_event`` is a bound method on the persona
# agent, but its ``CHANNEL_MESSAGE`` branch reads only ``event`` fields
# (verified against current code 2026-05-15 — RFC §D), so the unbound
# method formats a replayed peer turn without an agent instance. The cast
# pins the self-independent contract for type checkers; if a future
# refactor makes that branch touch ``self`` this seam must change with
# it. Reusing ``_format_event`` — rather than re-implementing the
# delimiter escape here — is the explicit RFC §D requirement.
_format_peer_message: Callable[[object, AgentEvent], str] = cast(
    "Callable[[object, AgentEvent], str]",
    _PromptAssemblyMixin._format_event,
)


async def build_conversation_messages(
    *,
    event: AgentEvent,
    agent_id: str,
    history_fetcher: ChannelHistoryFetcher,
    current_user_message: str,
    config: ConversationWindowConfig,
) -> list[dict[str, Any]]:
    """Return the LLM ``messages`` array seeded with the channel transcript.

    The last element is always the current event — ``current_user_message``
    is the caller's already-formatted current turn (the output of the
    persona's own ``_format_event``), appended verbatim and never dropped.
    Every earlier element is a sanitized replayed turn in chronological
    order.

    On a disabled config, a session-less / channel-less event, or any
    fetch failure, the result degrades to ``[current_event_only]`` — the
    persona is no worse off than it is without the conversation window.
    """
    current_turn: dict[str, Any] = {
        "role": "user",
        "content": current_user_message,
    }
    if not config.enabled:
        return [current_turn]

    channel_id = event.channel_id
    if not channel_id:
        # No channel scope (e.g. a TICK event) — nothing to reconstruct.
        return [current_turn]

    raw = await _fetch_window(
        history_fetcher=history_fetcher,
        channel_id=channel_id,
        message_id=event.message_id,
        # Over-fetch by one. If the inbound event is already persisted in
        # the channel store its own row is dropped from the window (dedup
        # by id in _assemble_replayed_turns); requesting max_turns + 1
        # keeps a full max_turns replayed turns either way. When the event
        # is not yet persisted the count cap in _apply_admission trims the
        # extra oldest row back off.
        limit=config.max_turns + 1,
    )
    if raw is None:
        return [current_turn]

    replayed = _assemble_replayed_turns(
        raw=raw,
        agent_id=agent_id,
        current_message_id=event.message_id,
        config=config,
    )
    return [*replayed, current_turn]


async def _fetch_window(
    *,
    history_fetcher: ChannelHistoryFetcher,
    channel_id: str,
    message_id: str | None,
    limit: int,
) -> list[dict[str, Any]] | None:
    """Return the raw channel-history rows, or ``None`` on fetch failure.

    Consults the in-process cache first (RFC §F). A Protocol exception
    degrades to ``None`` with a WARN; a ``None`` return from the fetcher
    is its own already-logged best-effort failure and degrades to
    ``None`` silently (no double log).
    """
    if message_id is not None:
        cached = _WINDOW_CACHE.get(channel_id)
        if cached is not None and cached[0] == message_id:
            return cached[1]

    try:
        raw = await history_fetcher.fetch(channel_id, limit=limit)
    except Exception as exc:
        logger.warning(
            "conversation window: history fetch raised for channel %s: %s",
            channel_id,
            exc,
            extra={
                "reason": "conversation_window_fetch_failed",
                "channel_id": channel_id,
            },
        )
        return None

    if raw is None:
        return None

    if message_id is not None:
        _WINDOW_CACHE[channel_id] = (message_id, raw)
    return raw


def _assemble_replayed_turns(
    *,
    raw: list[dict[str, Any]],
    agent_id: str,
    current_message_id: str | None,
    config: ConversationWindowConfig,
) -> list[dict[str, Any]]:
    """Map raw history rows to ``messages`` turns and apply admission.

    The history endpoint returns newest-first (RFC 0011 §C); rows are
    reversed so the transcript reads oldest→newest. The row carrying the
    current event's ``message_id`` is dropped — the caller appends the
    current event as the final turn (RFC §B).
    """
    turns: list[dict[str, Any]] = []
    for row in reversed(raw):
        if not isinstance(row, dict):
            continue
        if (
            current_message_id is not None
            and row.get("id") == current_message_id
        ):
            continue
        content = row.get("content")
        if not isinstance(content, str) or not content:
            continue
        sender_id = row.get("sender_id")
        if sender_id == agent_id:
            # The persona's own prior output is trusted — used raw as an
            # assistant turn, never wrapped in user-message delimiters.
            turns.append({"role": "assistant", "content": content})
        else:
            turns.append(
                {"role": "user", "content": _format_peer_turn(sender_id, content)},
            )
    return _apply_admission(turns, config)


def _format_peer_turn(sender_id: Any, content: str) -> str:
    """Format one replayed peer message as a user-turn string.

    Builds a synthetic ``CHANNEL_MESSAGE`` event and runs it through
    ``_format_event`` so the replayed turn inherits the exact
    ``<|user_message|>`` delimiter escape the in-flight event gets —
    the escape is never duplicated here (RFC §D).
    """
    synthetic = AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload={"content": content},
        sender_id=sender_id if isinstance(sender_id, str) else None,
        metadata={"sender_participant_type": "user"},
    )
    return _format_peer_message(None, synthetic)


def _apply_admission(
    turns: list[dict[str, Any]],
    config: ConversationWindowConfig,
) -> list[dict[str, Any]]:
    """Trim the replayed transcript to the configured bounds.

    OQ #2 resolution 2a: token-overflow FIFO first (drop oldest until the
    transcript fits ``max_tokens``), then count-overflow FIFO (drop
    oldest until at most ``max_turns`` remain). The caller's current
    event is appended afterwards and is never subject to this loop.
    """
    admitted = list(turns)
    tokens = [estimate_tokens(t["content"], accurate=True) for t in admitted]
    while admitted and sum(tokens) > config.max_tokens:
        admitted.pop(0)
        tokens.pop(0)
    while len(admitted) > config.max_turns:
        admitted.pop(0)
    return admitted
