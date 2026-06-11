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
* COMPLETE_TASK actions whose ``result`` is empty or whitespace-only,
  **on group channels** — silence is a valid turn outcome under the
  ``reply-discretion`` safety snippet; stamping a blank publish over a
  deliberate silence would defeat the affordance.

On **DM channels** the empty-reply case is not a no-op: the response
gate (``response_gate.py``) forces ``always`` on DMs and the
orchestrator's ``replyWaiter`` 504s if no publish arrives, so this
helper synthesises a minimal ellipsis placeholder to close the
round-trip. The ``reply-discretion`` snippet tells the persona this
directly; the fallback exists for the case where the LLM ignores the
guidance.

The synthesised action mentions the inbound ``sender_id`` so the
priority-1 chat-reply extraction in :mod:`agents.chat_reply` (legacy
``SendChatMessage`` path) picks the reply ahead of its priority-3
``COMPLETE_TASK.result`` fallback. Mentions are otherwise inert on DM
channels because DM membership is the routing primitive (see
:func:`agents.response_gate.evaluate_response_gate`).
"""

from __future__ import annotations

from ..persona_types import ActionType, AgentAction, AgentEvent, EventType

__all__ = ["bind_end_vote_channel", "synthesize_channel_reply"]


# DM channels enforce a "must reply" invariant (RFC 0011 §D,
# ``response_gate.py`` ``dm`` branch): a DM with no reply 504s the
# chat-as-DM REST round-trip on ``chatDefaultTimeout``. The
# ``reply-discretion`` prompt snippet tells the persona this directly,
# but when the LLM still produces no usable reply text — typically the
# model failed to follow the prompt — this fallback closes the loop
# with a minimal placeholder rather than leaving the user staring at a
# spinner. An ellipsis is unambiguous in audit logs and naturalistic
# as a "I have nothing to add" signal; the root cause (model not
# following the discretion guidance) is the right place to fix it, not
# this backstop.
_DM_EMPTY_REPLY_FALLBACK: str = "…"

# DM channels are identified by the ``dm:`` channel-id prefix — same
# convention used by ``response_gate.py`` (see ``_DM_CHANNEL_PREFIX``).
# Keeping the prefix literal here rather than importing avoids a
# circular dependency between this module and the gate.
_DM_CHANNEL_PREFIX: str = "dm:"


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

    # RFC 0030 producer plan PR 2 (IP6): reconcile any END_INTERACTION_VOTE
    # with the inbound channel before the promotion logic below — the two
    # concerns share this seam because both bind parsed actions to the
    # channel the turn arrived on.
    actions = bind_end_vote_channel(event, actions)

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
        # Group channels: silence is a valid turn outcome — see the
        # ``reply-discretion`` safety snippet. Preserve the no-op path
        # so a persona that deliberately declined does not get its
        # silence stamped over with a publish.
        #
        # DM channels: silence is broken by construction (the
        # response gate forces ``always`` on DMs, and the orchestrator's
        # ``replyWaiter`` 504s if no publish arrives). Synthesize a
        # minimal placeholder so the round-trip closes cleanly.
        if not channel_id.startswith(_DM_CHANNEL_PREFIX):
            return actions
        reply_text = _DM_EMPTY_REPLY_FALLBACK

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


def bind_end_vote_channel(
    event: AgentEvent,
    actions: list[AgentAction],
) -> list[AgentAction]:
    """Bind a channel-less ``END_INTERACTION_VOTE`` to the inbound channel.

    The RFC 0030 Layer 4 vote (producer plan PR 2, IP6) is a real channel
    publish, so the executor needs a ``channel_id`` — but a persona votes on
    *the conversation it is in*, and requiring it to echo routing details
    back through the action payload invites transcription mistakes (a typo'd
    channel casts the vote into the wrong room, or nowhere). This seam
    stamps the inbound channel onto any vote that omits ``channel_id``,
    exactly the :func:`synthesize_channel_reply` posture for conversational
    replies. An explicit payload value is preserved; non-channel events
    (e.g. a TICK-emitted vote) are left untouched — the executor's
    ``no_channel_id`` status is the backstop there.

    Pure: returns a new list when a binding fires; actions themselves are
    re-created, never mutated.
    """
    if event.event_type is not EventType.CHANNEL_MESSAGE or not event.channel_id:
        return actions

    bound: list[AgentAction] = []
    changed = False
    for action in actions:
        if (
            action.action_type is ActionType.END_INTERACTION_VOTE
            and not str(action.payload.get("channel_id", "") or "").strip()
        ):
            bound.append(AgentAction(
                action_type=ActionType.END_INTERACTION_VOTE,
                payload={**action.payload, "channel_id": event.channel_id},
            ))
            changed = True
        else:
            bound.append(action)
    return bound if changed else actions
