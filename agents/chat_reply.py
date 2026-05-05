"""
Chat-reply extraction helper for ``AgentServiceServicer.SendChatMessage``.

Extracted from ``agents/server_servicers.py`` (RFC 0011 PR 4a-i) so the
servicer module stays under the 500-line review-friendly cap after the
``ReceiveChannelMessage`` real handler landed. No logic changes.
"""

from __future__ import annotations

import logging
import re

from .persona_types import ActionType, AgentAction

logger = logging.getLogger("Persatrix.agent.server")

__all__ = ["extract_chat_reply"]


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
