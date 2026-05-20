"""Unit tests for RFC 0023 PR 6 — channel-message wallet-lease wiring.

The persona action loop is the LLM-call site for receiver-side channel
messages (``ReceiveChannelMessage``). PR 6 wires the
``CHANNEL_MESSAGE`` origin: the action loop must pass
``cause=CAUSE_CHANNEL_MESSAGE`` to :meth:`LLMClient.create_message`
when the event is a real channel delivery (discriminated by the absence
of ``metadata["chat_session_id"]`` — that key is the chat-as-DM shape
from PR 4).

Two behaviours are pinned here:

1. **Cause tagging.** A receiver-side channel-message event reaches
   :meth:`LLMClient.create_message` with
   ``cause=CAUSE_CHANNEL_MESSAGE``. PR 4 left this arm on
   ``CAUSE_UNSPECIFIED`` so :meth:`LLMClient.create_message` skipped the
   wallet bracket; PR 6 flips it so the wallet attributes spend to the
   channel-message origin and the per-cause dashboards show the fifth
   and last LLM-call origin in [RFC 0023 §Goal #1].
2. **Gate-precedes-lease.** The RFC 0011 response gate is evaluated
   **before** the lease is acquired — only a positive gate decision
   leads to a lease, so the wallet never holds a lease during gate
   evaluation. A suppressed event must not contact the wallet at all.

Chat events (``CHANNEL_MESSAGE`` *with* ``chat_session_id``) keep
``CAUSE_CHAT`` (PR 4 invariant); this test pins that PR 6 does not
regress the discrimination.
"""

from __future__ import annotations

import copy
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.generated import wallet_pb2 as walletpb
from agents.llm_client import LLMClient, LLMResponse, StopReason, Usage
from agents.persona import create_persona_agent
from agents.persona_runtime import _LLMPersonaAgent
from agents.persona_runtime.memory_context import MemoryInjectionResult
from agents.persona_runtime.wallet_cause import cause_for_event
from agents.persona_types import AgentEvent, EventType
from agents.wallet_client import BudgetExceededError

# ─── Helpers ──────────────────────────────────────────────────────────────────


_PERSONA_CONFIG: dict[str, Any] = {
    "id": "channel-agent",
    "type": "persona",
    "name": "Channel Agent",
    "role": "Channel-message lease test fixture",
    "model": "claude-sonnet-4-6",
    "temperature": 0.3,
    "max_llm_calls": 5,
    "max_tokens": 256,
    "persona": {"background": "Test fixture.", "behavior": {}},
    "permissions": {"memory": {"read": True, "write": True}},
    "memory": {
        "db_path": ":memory:",
        "notes": {"max_notes": 100, "auto_reflect_after": 5},
    },
}


def _llm_response() -> LLMResponse:
    return LLMResponse(
        text='[{"action_type": "complete_task", "payload": {"result": "ok"}}]',
        stop_reason=StopReason.END_TURN,
        usage=Usage(input_tokens=12, output_tokens=8),
    )


def _make_client_with_recording_create() -> LLMClient:
    """Build an ``LLMClient`` whose ``create_message`` records its kwargs."""
    provider = AsyncMock()
    provider.name = "anthropic"
    provider.create_message = AsyncMock(return_value=_llm_response())
    provider.format_tool_definitions = MagicMock(return_value=[])
    provider.append_tool_round = MagicMock(return_value=[])
    client = LLMClient(provider)
    real_create_message = client.create_message
    calls: list[dict[str, Any]] = []

    async def _record(*args: Any, **kwargs: Any) -> LLMResponse:
        calls.append(dict(kwargs))
        return await real_create_message(*args, **kwargs)

    client.create_message = _record  # type: ignore[method-assign]
    client._recorded_calls = calls  # type: ignore[attr-defined]
    return client


async def _make_agent(client: LLMClient) -> _LLMPersonaAgent:
    agent = create_persona_agent(
        agent_id="channel-agent",
        config=copy.deepcopy(_PERSONA_CONFIG),
        llm_client=client,
    )
    await agent.initialize_memory()
    return agent


def _channel_event(
    *,
    channel_id: str = "chan-xyz",
    sender_id: str = "peer-1",
    respond_policy: str = "always",
    mentions: list[str] | None = None,
) -> AgentEvent:
    """An ``AgentEvent`` shaped like ``ReceiveChannelMessage`` builds.

    No ``chat_session_id`` metadata — that is the chat-as-DM discriminator.
    Carries ``channel_id`` / ``message_id`` and an ``always``
    ``respond_policy`` so the RFC 0011 response gate admits the event.
    """
    return AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload={
            "content": "hi from channel",
            "channel_type": "general",
            "mentions": mentions or [],
            "respond_policy": respond_policy,
            "thread_parent_sender_id": "",
        },
        channel_id=channel_id,
        sender_id=sender_id,
        message_id="msg-1",
    )


def _chat_event() -> AgentEvent:
    """An ``AgentEvent`` shaped like ``SendChatMessage`` builds (PR 4 path)."""
    return AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload={"content": "hi", "user_id": "user-1", "participant_type": "user"},
        sender_id="user-1",
        metadata={
            "chat_session_id": "chat-123",
            "sender_participant_type": "user",
        },
    )


def _nonzero_injection() -> MemoryInjectionResult:
    """Force the empty-context TICK short-circuit *not* to fire; receiver-side
    channel events do not exercise that branch (it's gated on ``EventType.TICK``)
    but keep the helper symmetric with the chat/tick lease test suites."""
    return MemoryInjectionResult(memory_admitted_tokens=10)


# ─── Pure cause_for_event discriminator tests ───────────────────────────────


class TestCauseForEventChannelMessage:
    """The free function ``cause_for_event`` is the discriminator both PR 4
    and PR 6 rely on. Pin its behaviour here so a regression surfaces without
    a full action-loop spin-up."""

    def test_receiver_side_channel_event_maps_to_channel_message(self) -> None:
        """No ``chat_session_id`` → ``CAUSE_CHANNEL_MESSAGE`` (PR 6)."""
        event = _channel_event()
        assert cause_for_event(event) == walletpb.CAUSE_CHANNEL_MESSAGE, (
            "PR 6: receiver-side channel events must map to "
            "CAUSE_CHANNEL_MESSAGE (PR 4 left this arm on CAUSE_UNSPECIFIED)"
        )

    def test_chat_event_still_maps_to_chat(self) -> None:
        """``chat_session_id`` set → still ``CAUSE_CHAT`` (PR 4 invariant)."""
        assert cause_for_event(_chat_event()) == walletpb.CAUSE_CHAT, (
            "PR 4 invariant: chat-as-DM events must stay on CAUSE_CHAT"
        )


# ─── Cause-tagging tests at the action-loop call site ───────────────────────


class TestChannelPathCauseTagging:
    """The action loop must pass ``cause=CAUSE_CHANNEL_MESSAGE`` for
    receiver-side channel events so the wallet bracket fires."""

    @pytest.mark.asyncio
    async def test_channel_event_passes_cause_channel_message(self) -> None:
        client = _make_client_with_recording_create()
        agent = await _make_agent(client)
        try:
            with patch.object(
                agent, "_inject_memory_context", return_value=_nonzero_injection(),
            ):
                await agent.on_event(_channel_event())

            calls: list[dict[str, Any]] = client._recorded_calls  # type: ignore[attr-defined]
            assert calls, "create_message must be invoked for a channel event"
            first = calls[0]
            assert first.get("cause") == walletpb.CAUSE_CHANNEL_MESSAGE, (
                "PR 6: receiver-side channel events must tag the lease with "
                "CAUSE_CHANNEL_MESSAGE so the wallet attributes spend correctly "
                f"(got {first.get('cause')!r})"
            )
            assert first.get("agent_id") == "channel-agent", (
                "PR 6: channel lease must be acquired against the persona's agent_id"
            )
        finally:
            await agent.close_memory()

    @pytest.mark.asyncio
    async def test_chat_event_still_tagged_chat_post_pr6(self) -> None:
        """PR 6 must not regress PR 4's chat → ``CAUSE_CHAT`` discrimination."""
        client = _make_client_with_recording_create()
        agent = await _make_agent(client)
        try:
            with patch.object(
                agent, "_inject_memory_context", return_value=_nonzero_injection(),
            ):
                await agent.on_event(_chat_event())

            calls: list[dict[str, Any]] = client._recorded_calls  # type: ignore[attr-defined]
            assert calls
            assert calls[0].get("cause") == walletpb.CAUSE_CHAT, (
                "PR 4 invariant: PR 6 must keep chat events on CAUSE_CHAT "
                f"(got {calls[0].get('cause')!r})"
            )
        finally:
            await agent.close_memory()


# ─── Gate-precedes-lease invariant ──────────────────────────────────────────


class TestGatePrecedesLease:
    """RFC 0023 § Phased Implementation Plan Phase 6 — the response gate
    runs *before* the lease is acquired. A gated-out channel event must
    not call ``create_message`` at all, so the wallet never sees a lease
    for a message the persona declined to answer."""

    @pytest.mark.asyncio
    async def test_gated_out_event_does_not_create_message(self) -> None:
        """``when_mentioned`` policy with no mention → gate suppresses → no
        ``create_message`` invocation (and therefore no lease acquired)."""
        client = _make_client_with_recording_create()
        agent = await _make_agent(client)
        try:
            event = _channel_event(respond_policy="when_mentioned", mentions=[])
            with patch.object(
                agent, "_inject_memory_context", return_value=_nonzero_injection(),
            ):
                await agent.on_event(event)

            calls: list[dict[str, Any]] = client._recorded_calls  # type: ignore[attr-defined]
            assert calls == [], (
                "PR 6: a gated-out channel event must not call create_message — "
                "the gate runs before lease acquisition, so the wallet never "
                f"sees a lease for a suppressed message (got {len(calls)} calls)"
            )
        finally:
            await agent.close_memory()

    @pytest.mark.asyncio
    async def test_gate_admitted_event_does_acquire_lease(self) -> None:
        """``always`` policy → gate admits → ``create_message`` called once
        with the channel-message cause (lease acquired)."""
        client = _make_client_with_recording_create()
        agent = await _make_agent(client)
        try:
            with patch.object(
                agent, "_inject_memory_context", return_value=_nonzero_injection(),
            ):
                await agent.on_event(_channel_event(respond_policy="always"))

            calls: list[dict[str, Any]] = client._recorded_calls  # type: ignore[attr-defined]
            assert len(calls) == 1, (
                "PR 6: a gate-admitted channel event must call create_message "
                f"exactly once (got {len(calls)})"
            )
            assert calls[0].get("cause") == walletpb.CAUSE_CHANNEL_MESSAGE
        finally:
            await agent.close_memory()


# ─── Budget-denied channel event propagates ─────────────────────────────────


class TestChannelBudgetDenialPropagates:
    """A wallet denial on a channel-message reply must propagate as
    :class:`BudgetExceededError`. Unlike TICK (where there is no caller
    to render the denial and the loop short-circuits to ``DO_NOTHING``),
    a channel-message has the channel itself as the failure surface: the
    recipient ``BaseAgent.on_event`` handler reports the failure, and the
    orchestrator-side router records the denial — but the agent must not
    swallow it into a ``COMPLETE_TASK("LLM provider error")`` which would
    look like a normal provider outage.
    """

    @pytest.mark.asyncio
    async def test_channel_budget_denial_raises(self) -> None:
        provider = AsyncMock()
        provider.name = "anthropic"
        provider.create_message = AsyncMock(
            side_effect=BudgetExceededError(
                "per_agent budget exceeded",
                scope="per_agent",
                spent_usd=10.0,
                limit_usd=10.0,
                estimated_usd=0.01,
                reason="budget_exceeded",
            ),
        )
        provider.format_tool_definitions = MagicMock(return_value=[])
        provider.append_tool_round = MagicMock(return_value=[])
        client = LLMClient(provider)
        agent = await _make_agent(client)
        try:
            with patch.object(
                agent, "_inject_memory_context", return_value=_nonzero_injection(),
            ):
                with pytest.raises(BudgetExceededError) as excinfo:
                    await agent.on_event(_channel_event())
            assert excinfo.value.scope == "per_agent"
        finally:
            await agent.close_memory()
