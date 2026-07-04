"""RFC 0023 — wallet-lease ``cause`` derivation for the persona action loop.

Free-function helpers (no ``self`` access) split out of ``action_loop.py``
so that file stays under the 500-line review limit, mirroring the
``channel_ingest`` / ``channel_reply`` split convention.
"""

from __future__ import annotations

from ..base import TaskInput
from ..channel_wire_metadata import wire_interaction_id
from ..generated import wallet_pb2 as walletpb
from ..persona_types import AgentEvent, EventType

__all__ = [
    "cause_for_event",
    "lease_attribution_for_event",
    "lease_interaction_id_for_event",
]


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


def lease_attribution_for_event(
    event: AgentEvent,
    *,
    agent_id: str,
) -> tuple[walletpb.Cause.ValueType, str, str]:
    """Return ``(cause, lease_agent_id, interaction_id)`` for the loop's LLM call.

    Layers the ISSUE-0064 persona-as-sub-agent override on top of
    :func:`cause_for_event`. When a ``TASK_ASSIGNED`` event carries a
    :class:`~agents.task_types.TaskInput` whose
    ``config.sub_agent_parent_id`` is non-empty, the spawner
    (:class:`agents.sub_agents.spawner.SubAgentSpawner`) marked the
    dispatch as a sub-agent invocation. The lease must then be tagged
    ``CAUSE_SUB_AGENT`` and attributed to the parent's ``agent_id`` —
    exact twin of the override RFC 0023 PR 5 added to
    :meth:`agents.base.BaseAgent._run_llm_loop`. Otherwise the cause/agent
    pair is ``(cause_for_event(event), agent_id)``.

    The third element is :func:`lease_interaction_id_for_event` (RFC 0030
    producer plan PR 2): the orchestrator-resolved interaction the quality
    turn bills to, ``""`` when untracked. Bundled here rather than read at
    the call site so the loop's lease attribution stays one call.
    """
    cause = cause_for_event(event)
    lease_agent_id = agent_id
    if event.event_type is EventType.TASK_ASSIGNED:
        # AgentEvent.payload is dataclass-typed ``dict[str, Any]`` and
        # PersonaAgent.handle wraps the task as ``payload={"task": task}``
        # (agents/persona.py), so .get() is contract-safe. The
        # ``isinstance(task, TaskInput)`` guard mirrors prompt_assembly.py's
        # convention for narrowing the ``Any``-typed value before reaching
        # into ``.config.sub_agent_parent_id``.
        task = event.payload.get("task")
        if isinstance(task, TaskInput) and task.config.sub_agent_parent_id:
            cause = walletpb.CAUSE_SUB_AGENT
            lease_agent_id = task.config.sub_agent_parent_id
    return cause, lease_agent_id, lease_interaction_id_for_event(event)


def lease_interaction_id_for_event(event: AgentEvent) -> str:
    """The RFC 0020 ``interaction_id`` the event's leased LLM calls bill to.

    The interaction-id producer (RFC 0030 producer plan, PR 1) stamps the
    orchestrator-resolved id onto every routed publish; the gRPC servicer
    lifts it onto the event metadata (``seed_wire_metadata``), and this
    helper is the loop-side read — threaded into the Tier C quality-turn
    lease and the Tier B salience-bid lease (producer plan PR 2), the
    Layer 1 substrate: the wallet acts on the id only once a positive
    ``interaction_budget_tokens`` accompanies it on the same lease request
    (the config-stamping follow-up). The key literal
    mirrors Go's ``interactionIDMetadataKey``
    (internal/channels/interaction_id.go); the cross-language drift pin
    keeps the two in lockstep.

    Absent (legacy / pre-producer / TICK) and non-string values resolve to
    the untracked empty string — ``WalletClient.lease`` treats that as "no
    interaction attribution", every ceiling at its uncapped default. The
    same tolerance as every other metadata read at this boundary.

    Delegates to the shared drift-pinned reader (PR #716 review — this was a
    byte-identical inline copy): the lease id this returns is what interaction
    spend bills under, and the router's soft-budget bounded-close trigger
    reads spend by the id the no-reopen claim carries, so the two reads
    diverging would leave the cost close blind to the very spend it bounds.
    """
    return wire_interaction_id(event)
