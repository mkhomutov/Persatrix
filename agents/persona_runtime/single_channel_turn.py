"""RFC 0038 §B single-channel-turn guard — carved into RFC 0037 (PR 4).

The §D structural guarantee reads "a turn acting in channel ``C`` …
can publish only to ``C``" — but until this guard, that clause described
the intended runtime, not the code: ``SEND_CHANNEL_MESSAGE`` carries its
own ``channel_id`` payload and the pure ``validate_action_payload``
checks only that it is non-empty, so a turn could publish to any channel
the persona belongs to, straight past a gate keyed on the *inbound*
channel.  Per the 2026-07-19 decision (RFC 0037 Decision #3) the RFC 0038
§B guard ships here, with/after the §D gate whose tick exception it
presumes, so the eventual RFC 0038 relay (§E, v0.4.0+) extends rather
than amends it.

Event-aware and post-parse — a sibling of ``synthesize_channel_reply``
(the pure payload validator cannot see the event): a **channel-anchored**
turn's ``SEND_CHANNEL_MESSAGE`` whose target differs from the acting
channel is replaced with ``DO_NOTHING`` + WARNING.  The
``channel.cross_channel_publish_rejected`` audit event ships
WARNING-log-first — the agent-side RFC 0009 audit emission path is not
yet wired (the orchestrator owns it); the audit wire-up is a tracked
follow-up, not silent scope growth.

The tick-shaped class (``PUBLIC_FLOOR_EVENT_TYPES``) is exempt: those
turns may publish anywhere because their memory injection is already
gated to the §D ``public`` floor, so nothing above ``public`` can be in
their context for any target to receive.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..persona_types import ActionType, AgentAction
from .injection_gate import CHANNEL_ACTING_EVENT_TYPES

if TYPE_CHECKING:
    from ..persona_types import AgentEvent

logger = logging.getLogger(__name__)

__all__ = ["enforce_single_channel_turn"]


def enforce_single_channel_turn(
    event: AgentEvent,
    actions: list[AgentAction],
    *,
    agent_id: str,
) -> list[AgentAction]:
    """Replace cross-channel publishes from a channel-anchored turn.

    Only ``SEND_CHANNEL_MESSAGE`` actions with a non-empty string target
    different from ``event.channel_id`` are rejected; a missing/empty
    target stays for ``validate_action_payload``'s existing rejection,
    and every other action type passes through untouched.  Returns a new
    list (never mutates ``actions``) with each rejected action replaced
    by ``DO_NOTHING`` so downstream accounting still sees one entry per
    parsed action.
    """
    if event.event_type not in CHANNEL_ACTING_EVENT_TYPES or not event.channel_id:
        return actions
    guarded: list[AgentAction] = []
    for action in actions:
        if action.action_type is ActionType.SEND_CHANNEL_MESSAGE:
            target = action.payload.get("channel_id")
            if isinstance(target, str) and target and target != event.channel_id:
                logger.warning(
                    "Agent %s: cross-channel publish rejected — "
                    "SEND_CHANNEL_MESSAGE targeted %r from a turn acting "
                    "in %r (RFC 0038 §B single-channel-turn; audit "
                    "event wire-up is a tracked follow-up)",
                    agent_id, target, event.channel_id,
                )
                guarded.append(AgentAction(
                    action_type=ActionType.DO_NOTHING, payload={},
                ))
                continue
        guarded.append(action)
    return guarded
