"""Unit tests for RFC 0023 PR 4 — chat-path wallet-lease wiring.

The persona action loop is the LLM-call site for both ``SendChatMessage``
(via the ``EventDispatcher.dispatch`` path) and ``ReceiveChannelMessage``
(PR 6). PR 4 wires the *chat* origin only: the action loop must pass
``cause=CAUSE_CHAT`` to :meth:`LLMClient.create_message` when the event
was built by ``SendChatMessage`` (discriminated by a ``chat_session_id``
metadata key — set by :meth:`AgentServiceServicer.SendChatMessage`,
absent on receiver-side channel events), and must let
:class:`BudgetExceededError` propagate so the chat handler can surface it
as ``ChatResponse.reply_status="error"`` instead of swallowing it into a
generic ``COMPLETE_TASK("LLM provider error")``.

The receiver-side channel-message origin flips to
``CAUSE_CHANNEL_MESSAGE`` in PR 6 — dedicated coverage lives in
``agents/tests/test_action_loop_channel_lease.py``. This file retains a
regression check that the chat discriminator (``chat_session_id``
metadata) still selects ``CAUSE_CHAT`` rather than collapsing to one
``CHANNEL_MESSAGE``-shaped event class.
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
from agents.persona_types import ActionType, AgentEvent, EventType
from agents.wallet_client import BudgetExceededError

# ─── Helpers ──────────────────────────────────────────────────────────────────


_PERSONA_CONFIG: dict[str, Any] = {
    "id": "chat-agent",
    "type": "persona",
    "name": "Chat Agent",
    "role": "Chat-path lease test fixture",
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
        text='[{"action_type": "send_channel_message", "payload": '
             '{"content": "Hello, human!", "target": "all"}}]',
        stop_reason=StopReason.END_TURN,
        usage=Usage(input_tokens=12, output_tokens=8),
    )


def _make_client_with_recording_create() -> LLMClient:
    """Build an LLMClient whose ``create_message`` records its kwargs.

    We assert on the ``cause`` / ``agent_id`` kwargs the action loop is
    expected to pass through after PR 4 wires the chat path.
    """
    provider = AsyncMock()
    provider.name = "anthropic"
    provider.create_message = AsyncMock(return_value=_llm_response())
    provider.format_tool_definitions = MagicMock(return_value=[])
    provider.append_tool_round = MagicMock(return_value=[])
    client = LLMClient(provider)
    # Wrap ``create_message`` so tests can inspect the kwargs the
    # action loop passes to it. We intentionally do not stub it — we
    # want the real wallet-or-no-wallet branch inside ``LLMClient`` to
    # execute, so the kwargs reflect the *call site*'s contract.
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
        agent_id="chat-agent",
        config=copy.deepcopy(_PERSONA_CONFIG),
        llm_client=client,
    )
    await agent.initialize_memory()
    return agent


def _chat_event(*, content: str = "hello", chat_session_id: str = "chat-123") -> AgentEvent:
    """An ``AgentEvent`` shaped like the one ``SendChatMessage`` builds.

    The discriminating signal is ``metadata["chat_session_id"]``: it is
    set unconditionally on the chat path (RFC 0016 OQ 9) and never set on
    the receiver-side ``ReceiveChannelMessage`` path. Tests rely on that
    invariant — keep the chat handler's metadata in sync if it changes.
    """
    return AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload={"content": content, "user_id": "user-1", "participant_type": "user"},
        sender_id="user-1",
        metadata={
            "chat_session_id": chat_session_id,
            "sender_participant_type": "user",
        },
    )


def _channel_event(*, content: str = "hi from channel") -> AgentEvent:
    """An ``AgentEvent`` shaped like ``ReceiveChannelMessage`` builds.

    No ``chat_session_id``; carries a ``channel_id`` and ``message_id``.
    PR 6 wires this path through ``CAUSE_CHANNEL_MESSAGE``.
    """
    return AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload={
            "content": content,
            "channel_type": "general",
            "mentions": [],
            "respond_policy": "always",
            "thread_parent_sender_id": "",
        },
        channel_id="chan-xyz",
        sender_id="peer-1",
        message_id="msg-1",
    )


def _zero_injection() -> MemoryInjectionResult:
    return MemoryInjectionResult(memory_admitted_tokens=0)


# ─── Cause-tagging tests ─────────────────────────────────────────────────────


class TestChatPathCauseTagging:
    """The action loop must pass ``cause=CAUSE_CHAT`` for chat events."""

    @pytest.mark.asyncio
    async def test_chat_event_passes_cause_chat_to_create_message(self) -> None:
        """``SendChatMessage``-shaped events → ``cause=CAUSE_CHAT``."""
        client = _make_client_with_recording_create()
        agent = await _make_agent(client)

        with patch.object(
            agent, "_inject_memory_context", return_value=_zero_injection(),
        ):
            await agent.on_event(_chat_event())

        calls: list[dict[str, Any]] = client._recorded_calls  # type: ignore[attr-defined]
        assert calls, "create_message must be invoked for a chat event"
        first = calls[0]
        assert first.get("cause") == walletpb.CAUSE_CHAT, (
            "PR 4: chat events must tag the lease with CAUSE_CHAT so the wallet "
            f"attributes spend correctly (got {first.get('cause')!r})"
        )
        assert first.get("agent_id") == "chat-agent", (
            "PR 4: chat lease must be acquired against the persona's agent_id"
        )

    @pytest.mark.asyncio
    async def test_channel_event_tagged_channel_message_post_pr6(self) -> None:
        """Receiver-side channel events tagged ``CAUSE_CHANNEL_MESSAGE`` (PR 6).

        Kept in this file as the negative-of-the-positive: PR 4's chat
        discriminator (``chat_session_id`` metadata) must select
        ``CAUSE_CHAT`` rather than ``CAUSE_CHANNEL_MESSAGE``; without
        this regression check a refactor that erases the discriminator
        would silently route all channel-shaped events to one cause.
        The dedicated PR 6 coverage lives in
        ``agents/tests/test_action_loop_channel_lease.py``.
        """
        client = _make_client_with_recording_create()
        agent = await _make_agent(client)

        with patch.object(
            agent, "_inject_memory_context", return_value=_zero_injection(),
        ):
            await agent.on_event(_channel_event())

        calls: list[dict[str, Any]] = client._recorded_calls  # type: ignore[attr-defined]
        assert calls, "create_message must be invoked for a channel event"
        first = calls[0]
        assert first.get("cause") == walletpb.CAUSE_CHANNEL_MESSAGE, (
            "PR 6: receiver-side channel events must tag the lease with "
            "CAUSE_CHANNEL_MESSAGE (the chat discriminator must not select "
            f"this arm). Got cause={first.get('cause')!r}"
        )


# ─── BudgetExceededError propagation ─────────────────────────────────────────


class TestChatPathBudgetErrorPropagation:
    """The action loop must let ``BudgetExceededError`` escape for chat events.

    The chat handler in ``AgentServiceServicer.SendChatMessage`` catches the
    error and surfaces it as ``ChatResponse.reply_status="error"`` with the
    wallet's ``LeaseDenied.message``. If the action loop swallows it into a
    ``COMPLETE_TASK`` action the chat client would see a normal reply
    carrying the budget-denied message text — losing the structured signal.
    """

    @pytest.mark.asyncio
    async def test_budget_exceeded_propagates_out_of_action_loop(self) -> None:
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

        with patch.object(
            agent, "_inject_memory_context", return_value=_zero_injection(),
        ):
            with pytest.raises(BudgetExceededError) as excinfo:
                await agent.on_event(_chat_event())

        assert excinfo.value.scope == "per_agent"
        assert "budget exceeded" in str(excinfo.value).lower()

    @pytest.mark.asyncio
    async def test_generic_provider_error_still_caught_as_complete_task(self) -> None:
        """Non-budget exceptions still degrade gracefully — PR 4 must not break that.

        A bare provider outage (network, 5xx, etc.) should still produce a
        ``COMPLETE_TASK`` action so the dispatcher / chat handler can render
        a generic error reply, exactly as in v0.2.3. Only
        :class:`BudgetExceededError` is newly distinguished.
        """
        provider = AsyncMock()
        provider.name = "anthropic"
        provider.create_message = AsyncMock(side_effect=RuntimeError("provider 503"))
        provider.format_tool_definitions = MagicMock(return_value=[])
        provider.append_tool_round = MagicMock(return_value=[])
        client = LLMClient(provider)
        agent = await _make_agent(client)

        with patch.object(
            agent, "_inject_memory_context", return_value=_zero_injection(),
        ):
            actions = await agent.on_event(_chat_event())

        assert len(actions) == 1
        assert actions[0].action_type == ActionType.COMPLETE_TASK
        assert "LLM provider error" in actions[0].payload.get("result", "")
