"""
Chat-reply extraction helper for ``AgentServiceServicer.SendChatMessage``.

Extracted from ``agents/server_servicers.py`` (RFC 0011 PR 4a-i) so the
servicer module stays under the 500-line review-friendly cap after the
``ReceiveChannelMessage`` real handler landed. No logic changes.

ISSUE-0065 added ``publish_chat_error_on_channel`` — the channel-receive
arm's equivalent of :func:`chat_error_response`, used by
``AgentServiceServicer._dispatch_channel_event`` when the persona
action loop raises :class:`BudgetExceededError`.
"""

from __future__ import annotations

import logging
import re

from .channel_publisher import ChannelPublisher
from .generated import task_pb2
from .persona_types import ActionType, AgentAction

logger = logging.getLogger("Persatrix.agent.server")

__all__ = [
    "chat_error_response",
    "extract_chat_reply",
    "publish_chat_error_on_channel",
]


def chat_error_response(
    agent_id: str, *, chat_session_id: str = "", reply: str = "",
) -> task_pb2.ChatResponse:
    """Build the ``reply_status='error'`` ChatResponse for an error path.

    Centralises the v0.3.2 shape so the validation arms and the RFC 0023
    PR 4 ``BudgetExceededError`` handler agree on what an error reply
    looks like on the wire.
    """
    return task_pb2.ChatResponse(
        agent_id=agent_id, chat_session_id=chat_session_id,
        reply=reply, reply_status="error",
    )


def extract_chat_reply(
    actions: list[AgentAction],
    user_id: str,
) -> tuple[str, str]:
    """Extract a chat reply text from a list of agent actions.

    Priority (OQ 5):
    1. ``SEND_CHANNEL_MESSAGE`` whose ``mentions`` list contains ``user_id``.
    2. Any ``SEND_CHANNEL_MESSAGE`` action.
    3. ``COMPLETE_TASK`` result payload.
    4. Empty string (reply_status="empty").

    Returns ``(reply_text, reply_status)`` where ``reply_status`` is one of
    ``"ok"``, ``"empty"``.
    """
    def _sanitize_reply(text: str) -> str:
        """Strip internal delimiter tags that should never be visible to users.

        The persona runtime wraps user messages in ``<|user_message …|>`` /
        ``<|/user_message|>`` delimiters for prompt-injection mitigation.
        If the LLM echoes these back in its response, strip them so the
        raw markup never reaches the end user.
        """
        # Primary precise sweep: known ``user_message`` delimiter shapes.
        cleaned = re.sub(
            r"<\|/?user_message[^|]*\|>",
            "",
            text,
        )
        # Defense-in-depth: strip any other ``<|…|>`` token-like fragments
        # that might slip through if the runtime adds new delimiter names
        # (e.g. ``<|system|>``, ``<|assistant|>``) or if the LLM hallucinates
        # one.  Allows inner pipes (e.g. ``user_id="a|b"``) by using a
        # non-greedy body bounded to 128 chars to avoid catastrophically
        # eating real reply content that happens to contain ``|>``.
        cleaned = re.sub(
            r"<\|/?[a-zA-Z_].{0,128}?\|>",
            "",
            cleaned,
        )
        # Fallback: strip a torn opening fragment at the very end of the
        # string (no closing ``|>``), which can happen if the LLM cuts off
        # mid-tag.  Anchored to end-of-string so we don't touch legitimate
        # ``<|`` substrings elsewhere in the reply.
        cleaned = re.sub(
            r"<\|/?[a-zA-Z_][^|>\s]{0,64}\Z",
            "",
            cleaned,
        )
        return cleaned.strip()

    send_messages = [
        a for a in actions if a.action_type == ActionType.SEND_CHANNEL_MESSAGE
    ]

    # Priority 1: user-targeted SEND_CHANNEL_MESSAGE
    if user_id:
        for action in send_messages:
            mentions = action.payload.get("mentions", [])
            if user_id in mentions:
                return _sanitize_reply(action.payload.get("content", "")), "ok"

    # Priority 2: any SEND_CHANNEL_MESSAGE
    if send_messages:
        return _sanitize_reply(send_messages[0].payload.get("content", "")), "ok"

    # Priority 3: COMPLETE_TASK result
    complete = next(
        (a for a in actions if a.action_type == ActionType.COMPLETE_TASK), None
    )
    if complete is not None:
        result = complete.payload.get("result", "")
        return _sanitize_reply(result), "ok"

    # Priority 4: empty — only warn when the agent returned actions but
    # none were reply-extractable; an empty action list is expected for
    # agents that legitimately produce no reply (review fix: log noise).
    if actions:
        logger.warning("SendChatMessage: no reply action found in agent response")
    return "", "empty"


async def publish_chat_error_on_channel(
    publisher: ChannelPublisher | None,
    *,
    agent_id: str,
    channel_id: str | None,
    inbound_sender_id: str | None,
    reply: str,
    reason: str,
) -> None:
    """Publish a structured-error reply on a chat-bearing channel.

    ISSUE-0065 — channel-receive arm's equivalent of
    :func:`chat_error_response`. The published message carries
    ``metadata["reply_status"]="error"`` as the discriminator the REST
    chat handler reads to flip the JSON envelope from ``"ok"`` to
    ``"error"``. ``sender_id=agent_id`` wakes the orchestrator's
    ``replyWaiter`` (keyed on ``(channelID, awaitFromAgentID)``);
    ``cascade_depth=0`` because this is a chat reply, not a fanout.

    ``channel_id`` / ``inbound_sender_id`` are typed ``Optional`` to
    match ``AgentEvent``'s declared shape; a ``None`` ``channel_id`` is
    treated as a no-op (log only) because there is no channel to
    publish on. In practice ``_dispatch_channel_event`` only invokes
    this helper for ``EventType.CHANNEL_MESSAGE``, so the field is
    populated — the guard is purely a type-system safety net.

    Falls back to a log line when no publisher is wired so the caller
    does not crash; in that configuration the REST surface times out at
    504 as it did pre-fix.
    """
    if publisher is None:
        logger.warning(
            "channel chat-error: no publisher wired (agent=%s channel=%s)",
            agent_id, channel_id,
        )
        return
    if channel_id is None:
        logger.warning(
            "channel chat-error: event has no channel_id (agent=%s) — skipping publish",
            agent_id,
        )
        return
    try:
        await publisher.publish(
            channel_id=channel_id,
            sender_id=agent_id,
            content=reply,
            mentions=[inbound_sender_id] if inbound_sender_id else [],
            cascade_depth=0,
            metadata={"reply_status": "error", "error_reason": reason},
        )
    except Exception:  # noqa: BLE001 — never let the error-publish raise
        logger.exception(
            "channel chat-error publish failed (agent=%s channel=%s)",
            agent_id, channel_id,
        )
