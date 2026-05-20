"""RFC 0023 — wallet-lease ``cause`` derivation for the persona action loop.

Free-function helpers (no ``self`` access) split out of ``action_loop.py``
so that file stays under the 500-line review limit, mirroring the
``channel_ingest`` / ``channel_reply`` split convention.
"""

from __future__ import annotations

from ..generated import wallet_pb2 as walletpb
from ..persona_types import AgentEvent, EventType

__all__ = ["cause_for_event", "lease_attribution_for_event"]


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
      delivery → still ``CAUSE_UNSPECIFIED`` here; PR 6 flips it to
      ``CAUSE_CHANNEL_MESSAGE``.
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
        # PR 6 will flip this arm to CAUSE_CHANNEL_MESSAGE.
        return walletpb.CAUSE_UNSPECIFIED
    if event.event_type is EventType.TICK:
        return walletpb.CAUSE_AUTONOMOUS_TICK
    if event.event_type is EventType.TASK_ASSIGNED:
        return walletpb.CAUSE_WORKFLOW_TASK
    return walletpb.CAUSE_UNSPECIFIED


def lease_attribution_for_event(
    event: AgentEvent,
    *,
    agent_id: str,
) -> tuple[walletpb.Cause.ValueType, str]:
    """Return ``(cause, lease_agent_id)`` for the action-loop LLM call.

    Layers the ISSUE-0064 persona-as-sub-agent override on top of
    :func:`cause_for_event`. When a ``TASK_ASSIGNED`` event carries a
    :class:`~agents.task_types.TaskInput` whose
    ``config.sub_agent_parent_id`` is non-empty, the spawner
    (:class:`agents.sub_agents.spawner.SubAgentSpawner`) marked the
    dispatch as a sub-agent invocation. The lease must then be tagged
    ``CAUSE_SUB_AGENT`` and attributed to the parent's ``agent_id`` —
    exact twin of the override RFC 0023 PR 5 added to
    :meth:`agents.base.BaseAgent._run_llm_loop`. Otherwise the result is
    ``(cause_for_event(event), agent_id)``.
    """
    cause = cause_for_event(event)
    lease_agent_id = agent_id
    if event.event_type is EventType.TASK_ASSIGNED:
        task = event.payload.get("task") if isinstance(event.payload, dict) else None
        parent = getattr(getattr(task, "config", None), "sub_agent_parent_id", "") or ""
        if parent:
            cause = walletpb.CAUSE_SUB_AGENT
            lease_agent_id = parent
    return cause, lease_agent_id
