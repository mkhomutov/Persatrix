"""RFC 0023 — wallet-lease ``cause`` derivation for the persona action loop.

Free-function helper (no ``self`` access) split out of ``action_loop.py``
so that file stays under the 500-line review limit, mirroring the
``channel_ingest`` / ``channel_reply`` split convention.
"""

from __future__ import annotations

from ..generated import wallet_pb2 as walletpb
from ..persona_types import AgentEvent, EventType

__all__ = ["cause_for_event"]


def cause_for_event(event: AgentEvent) -> walletpb.Cause.ValueType:
    """Pick the RFC 0023 lease ``cause`` for an event handled by the loop.

    The persona action loop is the LLM-call site for chat
    (``SendChatMessage``), receiver-side channel messages
    (``ReceiveChannelMessage``), autonomous ticks, and workflow-step
    dispatch to a persona agent. They route through different wallet
    causes:

    * ``CHANNEL_MESSAGE`` with ``metadata["chat_session_id"]`` set is
      the chat servicer's shape (RFC 0016 OQ 9) → ``CAUSE_CHAT``.
    * ``CHANNEL_MESSAGE`` without that key is the receiver-side
      ``ReceiveChannelMessage`` delivery → ``CAUSE_CHANNEL_MESSAGE``
      (PR 6). The RFC 0011 response gate runs ahead of the LLM call in
      :meth:`_ActionLoopMixin._on_event_inner`, so a gated-out event
      returns ``DO_NOTHING`` before this discriminator is reached and
      the wallet never sees a lease for a suppressed message.
    * ``TICK`` → ``CAUSE_AUTONOMOUS_TICK`` (PR 5).
    * ``TASK_ASSIGNED`` → ``CAUSE_WORKFLOW_TASK`` (PR 5; ISSUE-0063).
      The scheduler's post-hoc ``recordStepUsage`` counter feed was
      retired in PR 3 on the assumption every workflow-step LLM call is
      leased; this arm makes it true for the persona-as-workflow-step
      path too.

    Anything else stays ``CAUSE_UNSPECIFIED``, which makes
    :meth:`LLMClient.create_message` skip the wallet bracket and behave
    exactly as in v0.2.3.
    """
    if event.event_type is EventType.CHANNEL_MESSAGE:
        if "chat_session_id" in event.metadata:
            return walletpb.CAUSE_CHAT
        return walletpb.CAUSE_CHANNEL_MESSAGE
    if event.event_type is EventType.TICK:
        return walletpb.CAUSE_AUTONOMOUS_TICK
    if event.event_type is EventType.TASK_ASSIGNED:
        return walletpb.CAUSE_WORKFLOW_TASK
    return walletpb.CAUSE_UNSPECIFIED
