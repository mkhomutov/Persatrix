"""
Chat-reply extraction helper for ``AgentServiceServicer.SendChatMessage``.

Extracted from ``agents/server_servicers.py`` (RFC 0011 PR 4a-i) so the
servicer module stays under the 500-line review-friendly cap after the
``ReceiveChannelMessage`` real handler landed. No logic changes.

ISSUE-0065 added ``publish_chat_error_on_channel`` — the channel-receive
arm's equivalent of :func:`chat_error_response`, used when the persona
action loop raises :class:`BudgetExceededError`.

ISSUE-0066 added ``dispatch_channel_event_with_chat_error_recovery`` —
the full chat-error-recovery wrapper around the persona action-loop
dispatch, hosting both the ``BudgetExceededError`` arm (ISSUE-0065) and
the gated ``AioRpcError(RESOURCE_EXHAUSTED)`` arm (ISSUE-0066) plus the
generic final-boundary logger. Extracted to keep ``server_servicers.py``
under the same line cap.

RFC 0024 Phase 4 added ``process_inbound_channel_event`` — the
fire-and-forget inbound processing body the per-agent ``EventLoop`` runs
via its ``on_inbound`` callback (and the ``EventDispatcher.enqueue_inbound``
no-loop fallback). It wraps the persona action loop in the recovery arm
above, which is why that arm now lives here rather than on the gRPC
servicer.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable
from typing import TYPE_CHECKING, Any

import grpc
import grpc.aio

from .channel_publisher import ChannelPublisher
from .channel_wire_metadata import wire_interaction_id
from .generated import task_pb2
from .persona_types import ActionType, AgentAction
from .wallet_client import BudgetExceededError

if TYPE_CHECKING:
    from .action_executor import ActionExecutor
    from .persona_runtime import _LLMPersonaAgent
    from .persona_types import AgentEvent

logger = logging.getLogger("Persatrix.agent.server")

__all__ = [
    "chat_error_response",
    "dispatch_channel_event_with_chat_error_recovery",
    "extract_chat_reply",
    "process_inbound_channel_event",
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
    publish on. In practice the channel-event processing path only
    invokes this helper for ``EventType.CHANNEL_MESSAGE``, so the field
    is populated — the guard is purely a type-system safety net.

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


async def dispatch_channel_event_with_chat_error_recovery(
    dispatch_coro: Awaitable[Any],
    *,
    publisher: ChannelPublisher | None,
    agent_id: str,
    channel_id: str | None,
    inbound_sender_id: str | None,
) -> None:
    """ISSUE-0065 / ISSUE-0066 — chat-error recovery wrapper for the
    persona action-loop dispatch fired from
    ``AgentServiceServicer.ReceiveChannelMessage``.

    Awaits ``dispatch_coro`` (typically ``EventDispatcher.dispatch(...)``)
    and converts the two known back-pressure exception classes —
    :class:`BudgetExceededError` (ISSUE-0065) and
    :class:`grpc.aio.AioRpcError` with ``code == RESOURCE_EXHAUSTED``
    (ISSUE-0066) — to a structured-error reply published on the
    originating channel via :func:`publish_chat_error_on_channel`. Other
    failures fall through to the generic final-boundary logger; silently
    turning every dispatch crash into a fake chat reply would mask
    genuine agent bugs (see the regression-guard
    ``test_generic_exception_does_not_publish_error_reply`` in
    ``agents/tests/test_chat_path_budget_denial.py``).

    Log-message prefix is hard-coded to ``"ReceiveChannelMessage"`` —
    this helper exists for exactly that surface, so the prefix is fixed
    rather than parameterised to keep log-line shapes stable for any
    downstream log search / alerting that grew on top of them. (No repo
    dashboard config currently selects on this prefix, but the field
    deliberately stays uniform across the BudgetExceededError /
    AioRpcError / generic arms so it can be relied on later.)
    """
    try:
        try:
            await dispatch_coro
        except BudgetExceededError as exc:
            logger.warning(
                "ReceiveChannelMessage budget-denied for agent %s "
                "(channel %s): scope=%s reason=%s message=%s",
                agent_id, channel_id,
                exc.scope or "<none>", exc.reason, exc.message,
            )
            await publish_chat_error_on_channel(
                publisher, agent_id=agent_id, channel_id=channel_id,
                inbound_sender_id=inbound_sender_id,
                reply=exc.message, reason=exc.reason,
            )
        except grpc.aio.AioRpcError as exc:
            if exc.code() != grpc.StatusCode.RESOURCE_EXHAUSTED:
                raise
            logger.warning(
                "ReceiveChannelMessage resource-exhausted for agent %s "
                "(channel %s): %s",
                agent_id, channel_id, exc.details(),
            )
            # Fixed reply — ``exc.details()`` leaks internal jargon
            # ("agent already holds the maximum 3 active leases").
            await publish_chat_error_on_channel(
                publisher, agent_id=agent_id, channel_id=channel_id,
                inbound_sender_id=inbound_sender_id,
                reply="Agent is at capacity — please retry in a moment.",
                reason="resource_exhausted",
            )
    except Exception as exc:  # noqa: BLE001 — final boundary
        logger.exception(
            "ReceiveChannelMessage dispatch failed for agent %s "
            "(channel %s): %s",
            agent_id, channel_id, type(exc).__name__,
        )


async def process_inbound_channel_event(
    *,
    agent: _LLMPersonaAgent,
    executor: ActionExecutor,
    event: AgentEvent,
    max_cascade_depth: int,
) -> None:
    """Process one fire-and-forget inbound channel event end-to-end.

    RFC 0024 Phase 4 inverts inbound dispatch: the per-agent
    :class:`agents.event_loop.EventLoop` owns the inbound-wake lifecycle
    — *decide → execute → recover* — instead of the gRPC handler awaiting
    a :class:`agents.event_loop.SyncDispatchHandle`. This is the single
    processing body shared by both fire-and-forget entry points:

    * the running-loop path — ``TickScheduler._handle_inbound_event``,
      wired as the loop's ``on_inbound`` callback; and
    * the no-running-loop fallback — ``EventDispatcher.enqueue_inbound``
      for an agent without a live supervisor (non-autonomous agents,
      test fixtures).

    The cascade-depth guard mirrors ``EventDispatcher.dispatch``: an event
    whose inbound depth already meets the ceiling is dropped *before* the
    agent runs, so a channel-message hop cannot re-arm a runaway cascade.
    The agent's actions execute at ``depth + 1`` so any
    ``SEND_CHANNEL_MESSAGE`` they produce publishes one hop deeper — the
    same increment the synchronous ``dispatch()`` path applied.

    The whole decide+execute span is wrapped in
    :func:`dispatch_channel_event_with_chat_error_recovery` so a
    :class:`BudgetExceededError` (ISSUE-0065) or an
    ``AioRpcError(RESOURCE_EXHAUSTED)`` (ISSUE-0066) raised inside the
    persona action loop still publishes a structured-error reply on the
    originating channel, rather than being swallowed by the event-loop
    supervisor's generic exception handler (which has no handle to reject
    on the fire-and-forget path).
    """
    depth = event.metadata.get("cascade_depth", 0)
    if depth >= max_cascade_depth:
        logger.warning(
            "Inbound cascade depth %d reached for agent %s, dropping event %s",
            depth, agent.agent_id, event.event_type.value,
        )
        return

    async def _decide_and_execute() -> None:
        actions = await agent.on_event(event)
        # RFC 0052 no-reopen claim (PR #716 review): thread the interaction id
        # this event was dispatched under (seeded off the wire by
        # ``seed_wire_metadata``) so a same-channel reply echoes it — the
        # latch's production input, read via the shared drift-pinned reader
        # (absent / non-string reads as untracked).
        await executor.execute(
            agent.agent_id, actions, cascade_depth=depth + 1,
            origin_channel_id=event.channel_id or "",
            origin_interaction_id=wire_interaction_id(event),
        )

    await dispatch_channel_event_with_chat_error_recovery(
        _decide_and_execute(),
        publisher=executor.channel_publisher,
        agent_id=agent.agent_id,
        channel_id=event.channel_id,
        inbound_sender_id=event.sender_id,
    )
