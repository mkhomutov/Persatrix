"""RFC 0011 PR 4b — channels response gate.

The gate decides whether the persona-runtime LLM should be invoked for
an inbound :class:`AgentEvent` whose :class:`EventType` is
``CHANNEL_MESSAGE``. It is the canonical enforcement point for the
per-membership ``respond_policy`` declared in
``schemas/channel.schema.json``.

The decision is made **pre-LLM, pre-memory-recall** in
:meth:`agents.persona_runtime._ActionLoopMixin._on_event_inner`, so a
suppressed message costs zero LLM tokens and zero retrieval round-trips.
Memory **ingestion** still runs in PR 5 — the gate's contract is "do
not respond", not "do not remember".

Policies (RFC 0011 §D table):

* ``when_mentioned`` — fire the LLM if the agent's id is in
  ``event.payload["mentions"]`` OR the message is a thread reply to a
  message this agent authored (``thread_parent_sender_id == agent_id``).
* ``always`` — fire the LLM unconditionally except when the agent is
  the sender (the orchestrator's :class:`ChannelRouter` already filters
  the sender on fanout, but the receiver re-checks for defence in depth
  on the cleartext gRPC port).
* ``never`` — always suppress. The orchestrator filters
  ``RespondNever`` members upstream of dispatch, so this branch should
  not normally fire; if it does, it surfaces a policy-routing
  regression and the gate suppresses to fail-closed.

DM channels are documented in the RFC 0011 §D table as
``always``-gated regardless of the per-membership knob (a DM with no
reply is broken by definition). The gate enforces this by treating
``channel_id`` starting with ``dm:`` as ``always``.

For non-CHANNEL_MESSAGE events the gate returns ``True`` unconditionally
— it has no opinion on TICK / TASK_ASSIGNED / etc.

Defense-in-depth ordering is preserved (RFC 0011 PR plan §PR 4 Key
implementation details): gate (primary) → existing
``EventDispatcher.max_cascade_depth=5`` (backstop) → REST-side rate
limit. The cascade-depth check fires *before* the gate in
:meth:`EventDispatcher.dispatch`, so the gate never sees an event past
the depth ceiling. The backstop is verified by
``tests/unit/python/test_response_gate_cascade_backstop.py``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Final

from .persona_types import AgentEvent, EventType

logger = logging.getLogger(__name__)

__all__ = [
    "POLICY_ALWAYS",
    "POLICY_NEVER",
    "POLICY_WHEN_MENTIONED",
    "GateDecision",
    "evaluate_response_gate",
]


# Policy-string constants pinned as ``Final`` so the gate, the proto
# validator, and the test suite all reference the same values.
POLICY_WHEN_MENTIONED: Final[str] = "when_mentioned"
POLICY_ALWAYS: Final[str] = "always"
POLICY_NEVER: Final[str] = "never"

_DM_CHANNEL_PREFIX: Final[str] = "dm:"


@dataclass(frozen=True, slots=True)
class GateDecision:
    """Outcome of :func:`evaluate_response_gate`.

    Attributes:
        respond: ``True`` when the persona runtime should proceed with
            memory recall + LLM invocation for this event; ``False`` when
            the gate suppresses the response.
        policy: The effective policy used for the decision (string
            value of :data:`POLICY_WHEN_MENTIONED` /
            :data:`POLICY_ALWAYS` / :data:`POLICY_NEVER`). Used as the
            ``policy`` label on the ``channel.messages.gated`` metric so
            operators can break suppression counts down by intent.
        reason: Short, low-cardinality string explaining the branch.
            Suitable for log fields and span attributes; never a free-form
            error string.
    """

    respond: bool
    policy: str
    reason: str


def evaluate_response_gate(event: AgentEvent, *, agent_id: str) -> GateDecision:
    """Decide whether the persona runtime should respond to ``event``.

    The function is **pure** — it consumes the event payload and the
    agent id, returns a :class:`GateDecision`, and does not mutate either
    input. The caller (``_on_event_inner``) emits metrics and logs based
    on the decision.

    Non-CHANNEL_MESSAGE events return ``respond=True`` unconditionally
    so callers can apply the gate uniformly without an event-type
    pre-check at every site.
    """
    if event.event_type is not EventType.CHANNEL_MESSAGE:
        return GateDecision(respond=True, policy="", reason="not_channel_message")

    # The legacy ``AgentService.SendChatMessage`` RPC builds a
    # CHANNEL_MESSAGE event without a ``channel_id`` (it predates the
    # chat-as-DM unification and is deferred for cleanup in
    # ``docs/issues/ISSUE-0035``). Until that issue lands, the gate
    # bypasses CHANNEL_MESSAGE events with an empty channel_id so the
    # legacy path keeps working — those events do not flow through the
    # channels subsystem and have no per-membership policy to enforce.
    channel_id = event.channel_id or ""
    if not channel_id:
        return GateDecision(respond=True, policy="", reason="no_channel_id")

    payload = event.payload or {}
    raw_policy = payload.get("respond_policy", "")
    policy = raw_policy if isinstance(raw_policy, str) else ""

    # DM channels override the per-membership policy: a DM with no reply
    # is broken by construction (RFC 0011 §D). The orchestrator-side
    # ``GetOrCreateDM`` always inserts both members with
    # ``RespondAlways``, so this override is consistent with the wire
    # value — but enforcing it explicitly here makes the gate robust to
    # an operator who hand-edits a DM membership row.
    if channel_id.startswith(_DM_CHANNEL_PREFIX):
        if event.sender_id == agent_id:
            return GateDecision(
                respond=False, policy=POLICY_ALWAYS, reason="dm_self_sender",
            )
        return GateDecision(respond=True, policy=POLICY_ALWAYS, reason="dm")

    # Sender-side filter (defence in depth). The router already drops
    # the sender on fanout; the gate re-checks because the cleartext
    # gRPC port cannot be trusted to carry a non-spoofed ``sender_id``.
    if event.sender_id == agent_id:
        return GateDecision(
            respond=False, policy=policy or POLICY_ALWAYS, reason="self_sender",
        )

    if policy == POLICY_NEVER:
        # Fail-closed. The orchestrator filters ``RespondNever`` members
        # upstream of dispatch, so a ``never`` reaching the gate is a
        # policy-routing regression — log at warn so operators see the
        # drift surface in their logs even though the gate already
        # suppressed the response.
        logger.warning(
            "Agent %s: respond_policy=never reached the gate (channel=%s); "
            "orchestrator should have filtered upstream",
            agent_id, channel_id,
        )
        return GateDecision(respond=False, policy=POLICY_NEVER, reason="policy_never")

    if policy == POLICY_ALWAYS:
        return GateDecision(respond=True, policy=POLICY_ALWAYS, reason="policy_always")

    if policy == POLICY_WHEN_MENTIONED:
        mentions = payload.get("mentions") or []
        if isinstance(mentions, list) and agent_id in mentions:
            return GateDecision(
                respond=True, policy=POLICY_WHEN_MENTIONED, reason="mentioned",
            )
        # Thread-reply-to-self trigger (RFC 0011 §D table). Activates
        # when the agent authored the parent message of this thread,
        # even if the reply does not explicitly mention the agent. The
        # parent sender id is pre-resolved by the router so the gate
        # need not look the parent up itself.
        thread_id = event.thread_id
        thread_parent_sender_id = payload.get("thread_parent_sender_id", "")
        if (
            thread_id
            and thread_parent_sender_id == agent_id
        ):
            return GateDecision(
                respond=True,
                policy=POLICY_WHEN_MENTIONED,
                reason="thread_reply_to_self",
            )
        return GateDecision(
            respond=False,
            policy=POLICY_WHEN_MENTIONED,
            reason="not_mentioned",
        )

    # Unknown / empty policy — fail-closed with a warn. The wire-side
    # validator already rejects unknown values, so this branch should
    # not fire in production; it is a belt-and-braces guard for tests
    # or future additive policies that have not been wired through the
    # gate yet.
    logger.warning(
        "Agent %s: unknown respond_policy %r on channel %s; suppressing",
        agent_id, raw_policy, channel_id,
    )
    return GateDecision(respond=False, policy=policy, reason="unknown_policy")
