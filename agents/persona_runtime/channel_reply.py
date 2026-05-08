"""Channel-reply synthesis for plain-text persona responses (ISSUE-0048).

The persona runtime parses the LLM's free-text reply via
:func:`_ActionLoopMixin._parse_actions`. When the model emits the
documented JSON action schema (e.g. ``[{"action_type":
"send_channel_message", ...}]``), the parser yields an explicit
:class:`ActionType.SEND_CHANNEL_MESSAGE` action and the executor
publishes via the orchestrator REST surface. When the model instead
responds conversationally — which is the default for a persona that has
not been prompt-trained on the action schema — the parser folds the
text into a single :class:`ActionType.COMPLETE_TASK` action carrying the
reply in ``payload["result"]``.

Without this seam, the executor records ``status=completed`` for the
``COMPLETE_TASK`` and never reaches the channel-publish branch. The
orchestrator-side ``replyWaiter`` therefore never observes a publish
from the agent on the inbound DM channel, and the chat-as-DM REST
round-trip 504s on its ``chatDefaultTimeout``.

This helper closes that gap by promoting the conversational reply into
an explicit :class:`ActionType.SEND_CHANNEL_MESSAGE` bound to the
inbound channel. It is called once per turn, immediately after
``_parse_actions``, and is a no-op for any of:

* Non-channel events (TICK / TASK_ASSIGNED / SUB_AGENT_COMPLETED / …).
* CHANNEL_MESSAGE events with an empty ``channel_id`` — the deprecated
  ``SendChatMessage`` path (cleanup tracked in ISSUE-0035).
* Action lists that already contain a ``SEND_CHANNEL_MESSAGE`` for the
  inbound channel — a well-prompted agent must not be double-published.
* COMPLETE_TASK actions whose ``result`` is empty or whitespace-only —
  publishing a blank reply is worse than 504ing.

The synthesised action mentions the inbound ``sender_id`` so the
priority-1 chat-reply extraction in :mod:`agents.chat_reply` (legacy
``SendChatMessage`` path) picks the reply ahead of its priority-3
``COMPLETE_TASK.result`` fallback. Mentions are otherwise inert on DM
channels because DM membership is the routing primitive (see
:func:`agents.response_gate.evaluate_response_gate`).
"""

from __future__ import annotations

from ..persona_types import ActionType, AgentAction, AgentEvent, EventType

__all__ = ["synthesize_channel_reply"]


def synthesize_channel_reply(
    event: AgentEvent,
    actions: list[AgentAction],
    agent_id: str,
) -> list[AgentAction]:
    """Promote a conversational COMPLETE_TASK reply into a channel publish.

    Pure function: the input ``actions`` list is not mutated. When
    synthesis fires the returned list contains a fresh
    ``SEND_CHANNEL_MESSAGE`` prepended in front of the original
    actions; otherwise the original list is returned unchanged.

    ``agent_id`` is accepted for parity with the action-loop call site
    (and for future audit logging keyed on the agent), but is not
    consumed by the current implementation — the executor stamps
    ``sender_id = agent_id`` on the wire publish via
    :meth:`agents.action_executor.ActionExecutor._handle_send_channel_message`,
    so the synthesised action does not carry it.
    """
    if event.event_type is not EventType.CHANNEL_MESSAGE:
        return actions

    channel_id = event.channel_id or ""
    if not channel_id:
        return actions

    for action in actions:
        if action.action_type is not ActionType.SEND_CHANNEL_MESSAGE:
            continue
        if action.payload.get("channel_id") == channel_id:
            return actions

    reply_text = ""
    for action in actions:
        if action.action_type is not ActionType.COMPLETE_TASK:
            continue
        candidate = action.payload.get("result", "")
        if isinstance(candidate, str) and candidate.strip():
            reply_text = candidate
            break

    if not reply_text:
        return actions

    mentions = [event.sender_id] if event.sender_id else []
    synthesized = AgentAction(
        action_type=ActionType.SEND_CHANNEL_MESSAGE,
        payload={
            "channel_id": channel_id,
            "content": reply_text,
            "mentions": mentions,
        },
    )
    return [synthesized, *actions]
