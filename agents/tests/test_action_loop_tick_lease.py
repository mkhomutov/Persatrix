"""Unit tests for RFC 0023 PR 5 — autonomous TICK wallet-lease wiring.

PR 5 wires the autonomous TICK origin (``cause=CAUSE_AUTONOMOUS_TICK``)
and adds a budget-denied → idle short-circuit so a wallet denial does
not crash the tick loop. The discriminator stays in
``wallet_cause.cause_for_event``: ``EventType.TICK`` now maps to
``CAUSE_AUTONOMOUS_TICK`` (was ``CAUSE_UNSPECIFIED`` after PR 4).

Two behaviours are pinned here:

1. **Cause tagging.** A TICK event reaches
   :meth:`LLMClient.create_message` with ``cause=CAUSE_AUTONOMOUS_TICK``
   so the wallet attributes spend to the autonomous-tick origin and
   the per-cause dashboards stop collapsing TICK into "unspecified".
2. **Budget-denied → idle short-circuit.** Unlike chat (where the
   handler surfaces the denial to the caller as
   ``reply_status="error"``), an autonomous TICK has no caller to
   notify. The loop must swallow :class:`BudgetExceededError` and
   return ``[DO_NOTHING]`` so ``TickScheduler`` increments
   ``idle_count`` via its existing ``all_do_nothing`` branch — same
   shape as the RFC 0017 §F empty-context short-circuit. A WARN log
   carries the wallet's denial message so sustained budget pressure is
   visible to operators; the ``persona_tick_idle`` counter records
   ``idle_reason=budget_denied`` so dashboards can separate
   budget-throttled idle from organic quiet periods.
"""

from __future__ import annotations

import copy
import logging
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

# ─── Fixtures ─────────────────────────────────────────────────────────────────


_PERSONA_CONFIG: dict[str, Any] = {
    "id": "tick-agent",
    "type": "persona",
    "name": "Tick Agent",
    "role": "Autonomous-tick lease test fixture",
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
        agent_id="tick-agent",
        config=copy.deepcopy(_PERSONA_CONFIG),
        llm_client=client,
    )
    await agent.initialize_memory()
    return agent


def _tick_event() -> AgentEvent:
    return AgentEvent(event_type=EventType.TICK, payload={})


def _nonzero_injection() -> MemoryInjectionResult:
    """Force the empty-context short-circuit *not* to fire so the LLM
    path is exercised — the short-circuit is an independent code path
    tested below."""
    return MemoryInjectionResult(memory_admitted_tokens=10)


def _zero_injection() -> MemoryInjectionResult:
    return MemoryInjectionResult(memory_admitted_tokens=0)


# ─── Cause-tagging tests ─────────────────────────────────────────────────────


class TestTickPathCauseTagging:
    """The action loop must pass ``cause=CAUSE_AUTONOMOUS_TICK`` for TICK."""

    @pytest.mark.asyncio
    async def test_tick_event_passes_cause_autonomous_tick(self) -> None:
        client = _make_client_with_recording_create()
        agent = await _make_agent(client)
        try:
            with patch.object(
                agent, "_inject_memory_context", return_value=_nonzero_injection(),
            ), patch.object(
                agent, "_has_active_goal_payload", return_value=True,
            ):
                await agent.on_event(_tick_event())

            calls: list[dict[str, Any]] = client._recorded_calls  # type: ignore[attr-defined]
            assert calls, "create_message must be invoked for a TICK event"
            first = calls[0]
            assert first.get("cause") == walletpb.CAUSE_AUTONOMOUS_TICK, (
                "PR 5: TICK events must tag the lease with CAUSE_AUTONOMOUS_TICK "
                f"so the wallet attributes spend correctly (got {first.get('cause')!r})"
            )
            assert first.get("agent_id") == "tick-agent", (
                "PR 5: tick lease must be acquired against the persona's agent_id"
            )
        finally:
            await agent.close_memory()


# ─── Budget-denied → idle short-circuit ──────────────────────────────────────


class TestTickPathBudgetDenialIdle:
    """A wallet denial on a TICK must short-circuit to DO_NOTHING.

    Chat propagates :class:`BudgetExceededError` because there is a
    caller to render the denial; an autonomous TICK has no such caller.
    Re-raising would surface as ``Tick error`` in
    :meth:`TickScheduler._run` and the tick would simply be lost rather
    than reflected as idle, blinding dashboards to budget pressure
    that the wallet is actually suppressing.
    """

    @pytest.mark.asyncio
    async def test_tick_budget_exceeded_returns_do_nothing(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
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
            ), patch.object(
                agent, "_has_active_goal_payload", return_value=True,
            ), caplog.at_level(logging.WARNING, logger="agents.persona_runtime.action_loop"):
                actions = await agent.on_event(_tick_event())

            assert len(actions) == 1, (
                "PR 5: budget-denied TICK must produce exactly one DO_NOTHING action"
            )
            assert actions[0].action_type == ActionType.DO_NOTHING, (
                "PR 5: budget-denied TICK must short-circuit to DO_NOTHING "
                "(re-raising would lose the tick instead of recording it idle)"
            )
            assert any(
                "budget" in record.message.lower() for record in caplog.records
            ), (
                "PR 5: budget-denied TICK must log a WARN so sustained budget "
                "pressure is visible to operators"
            )
        finally:
            await agent.close_memory()

    @pytest.mark.asyncio
    async def test_tick_budget_exceeded_does_not_propagate(self) -> None:
        """Re-raising would surface as 'Tick error' in TickScheduler._run."""
        provider = AsyncMock()
        provider.name = "anthropic"
        provider.create_message = AsyncMock(
            side_effect=BudgetExceededError(
                "wallet unreachable — LLM call failing closed",
                reason="wallet_unreachable",
            ),
        )
        provider.format_tool_definitions = MagicMock(return_value=[])
        provider.append_tool_round = MagicMock(return_value=[])
        client = LLMClient(provider)
        agent = await _make_agent(client)
        try:
            with patch.object(
                agent, "_inject_memory_context", return_value=_nonzero_injection(),
            ), patch.object(
                agent, "_has_active_goal_payload", return_value=True,
            ):
                # Must NOT raise — the contract is "swallow on TICK".
                actions = await agent.on_event(_tick_event())
            assert actions == [
                actions[0],
            ] and actions[0].action_type == ActionType.DO_NOTHING
        finally:
            await agent.close_memory()


# ─── idle_reason metric attribution ──────────────────────────────────────────


class TestTickIdleReasonMetric:
    """The ``persona_tick_idle`` counter must carry ``idle_reason``.

    Two surfaces increment it:

    * the existing RFC 0017 §F empty-context short-circuit with
      ``idle_reason=empty_context_tick``;
    * the PR 5 budget-denied TICK with ``idle_reason=budget_denied``.

    The counter lets dashboards separate budget-throttled idle ticks
    from organic quiet periods without joining traces.
    """

    @pytest.mark.asyncio
    async def test_budget_denied_tick_records_idle_reason(self) -> None:
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
            inst = MagicMock()
            inst.persona_tick_idle = MagicMock()
            inst.channel_messages_gated = MagicMock()
            inst.channel_messages_replayed = MagicMock()
            with patch.object(
                agent, "_inject_memory_context", return_value=_nonzero_injection(),
            ), patch.object(
                agent, "_has_active_goal_payload", return_value=True,
            ), patch(
                "agents.persona_runtime.action_loop.try_get_instruments",
                return_value=inst,
            ):
                await agent.on_event(_tick_event())

            assert inst.persona_tick_idle.add.called, (
                "PR 5: budget-denied TICK must increment persona_tick_idle"
            )
            call = inst.persona_tick_idle.add.call_args
            assert call.args[0] == 1
            attrs = call.kwargs.get("attributes") or (call.args[1] if len(call.args) > 1 else {})
            assert attrs.get("idle_reason") == "budget_denied", (
                "PR 5: budget-denied TICK must record idle_reason=budget_denied "
                f"(got attrs={attrs!r})"
            )
            assert attrs.get("agent.id") == "tick-agent"
        finally:
            await agent.close_memory()

    @pytest.mark.asyncio
    async def test_empty_context_tick_records_idle_reason(self) -> None:
        client = _make_client_with_recording_create()
        agent = await _make_agent(client)
        try:
            inst = MagicMock()
            inst.persona_tick_idle = MagicMock()
            inst.channel_messages_gated = MagicMock()
            inst.channel_messages_replayed = MagicMock()
            with patch.object(
                agent, "_inject_memory_context", return_value=_zero_injection(),
            ), patch.object(
                agent, "_has_active_goal_payload", return_value=False,
            ), patch.object(
                agent, "_has_pending_turn", return_value=False,
            ), patch(
                "agents.persona_runtime.action_loop.try_get_instruments",
                return_value=inst,
            ):
                actions = await agent.on_event(_tick_event())

            assert actions == [actions[0]] and actions[0].action_type == ActionType.DO_NOTHING
            assert inst.persona_tick_idle.add.called, (
                "PR 5: empty-context TICK must increment persona_tick_idle "
                "(the dashboard counterpart of the existing DEBUG log)"
            )
            call = inst.persona_tick_idle.add.call_args
            attrs = call.kwargs.get("attributes") or (call.args[1] if len(call.args) > 1 else {})
            assert attrs.get("idle_reason") == "empty_context_tick", (
                "PR 5: empty-context TICK must record idle_reason=empty_context_tick "
                f"(got attrs={attrs!r})"
            )
        finally:
            await agent.close_memory()


# ─── Non-TICK budget denials still propagate ─────────────────────────────────


class TestNonTickBudgetDenialStillPropagates:
    """The idle short-circuit must be scoped to TICK only.

    Chat, channel-message, and TASK_ASSIGNED events must continue to
    propagate :class:`BudgetExceededError` so their respective callers
    render the denial (chat → ``reply_status="error"``; workflow task →
    ``TaskStatus.FAILED`` with ``error_type=budget_exceeded``).
    """

    @pytest.mark.asyncio
    async def test_chat_budget_denial_still_propagates_post_pr5(self) -> None:
        provider = AsyncMock()
        provider.name = "anthropic"
        provider.create_message = AsyncMock(
            side_effect=BudgetExceededError(
                "per_agent budget exceeded",
                scope="per_agent",
                reason="budget_exceeded",
            ),
        )
        provider.format_tool_definitions = MagicMock(return_value=[])
        provider.append_tool_round = MagicMock(return_value=[])
        client = LLMClient(provider)
        agent = await _make_agent(client)
        try:
            chat_event = AgentEvent(
                event_type=EventType.CHANNEL_MESSAGE,
                payload={"content": "hi", "user_id": "u", "participant_type": "user"},
                sender_id="u",
                metadata={
                    "chat_session_id": "s-1",
                    "sender_participant_type": "user",
                },
            )
            with patch.object(
                agent, "_inject_memory_context", return_value=_nonzero_injection(),
            ):
                with pytest.raises(BudgetExceededError):
                    await agent.on_event(chat_event)
        finally:
            await agent.close_memory()
