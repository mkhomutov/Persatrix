"""Unit tests for ISSUE-0066 — action_loop layer must distinguish
``AioRpcError(RESOURCE_EXHAUSTED)`` from generic provider errors.

The wallet client retries `AcquireLease` on `codes.ResourceExhausted`
([`agents/wallet_client.py::_acquire`](../../agents/wallet_client.py))
and, on exhausting its retry budget, re-raises the raw
:class:`grpc.aio.AioRpcError`.  The exception propagates through
:meth:`LLMClient.create_message` into
:meth:`_LLMPersonaAgent._on_event_inner`'s LLM-call try-arm.

The fix shipped under PR #396 caught this at the dispatcher layer
(:func:`agents.chat_reply.dispatch_channel_event_with_chat_error_recovery`)
but the action loop's generic ``except Exception`` arm at
[`agents/persona_runtime/action_loop.py:423`](../../agents/persona_runtime/action_loop.py)
catches the :class:`AioRpcError` first and converts it to a
``COMPLETE_TASK`` action with ``result="LLM provider error"``.
``ActionExecutor.execute`` for ``COMPLETE_TASK`` returns a metadata
dict without publishing anything on the channel, so the orchestrator's
``PublishAndAwait`` reply waiter still times out at HTTP 504 — the
MT-COST-003 re-run on 2026-05-20 confirmed this live (see
[v0.3.2-execution-report.md §F-1](../../docs/manual-tests/v0.3.2-execution-report.md#follow-ups)).

The unit test in `agents/tests/test_chat_path_resource_exhausted.py`
passes because it mocks ``dispatcher.dispatch`` to raise the
``AioRpcError`` directly — that test pins the dispatcher's contract.
This module pins the *action-loop* contract one layer below: the
:class:`AioRpcError` must escape the action loop on chat/channel
paths (so the dispatcher's wrapper publishes the error reply) and
short-circuit to ``DO_NOTHING`` on TICK (symmetric to the existing
:class:`BudgetExceededError` arm).
"""

from __future__ import annotations

import copy
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import grpc
import grpc.aio
import pytest

from agents.llm_client import LLMClient
from agents.persona import create_persona_agent
from agents.persona_runtime import _LLMPersonaAgent
from agents.persona_runtime.memory_context import MemoryInjectionResult
from agents.persona_types import ActionType, AgentEvent, EventType

# ─── Fixtures (mirror test_action_loop_tick_lease.py / _chat_lease.py) ───────


_PERSONA_CONFIG: dict[str, Any] = {
    "id": "stress-agent",
    "type": "persona",
    "name": "Stress Agent",
    "role": "RESOURCE_EXHAUSTED action-loop test fixture",
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


def _rpc_error(
    code: grpc.StatusCode, *, details: str = "",
) -> grpc.aio.AioRpcError:
    """Construct an ``AioRpcError`` carrying *code* for stub side-effects.

    Mirrors the helper in ``test_chat_path_resource_exhausted.py``.
    """
    return grpc.aio.AioRpcError(
        code, grpc.aio.Metadata(), grpc.aio.Metadata(),
        details=details or f"simulated {code}",
    )


def _make_client_raising(exc: BaseException) -> LLMClient:
    """LLMClient whose ``create_message`` raises *exc* on first call."""
    provider = AsyncMock()
    provider.name = "anthropic"
    provider.create_message = AsyncMock(side_effect=exc)
    provider.format_tool_definitions = MagicMock(return_value=[])
    provider.append_tool_round = MagicMock(return_value=[])
    return LLMClient(provider)


async def _make_agent(client: LLMClient) -> _LLMPersonaAgent:
    agent = create_persona_agent(
        agent_id="stress-agent",
        config=copy.deepcopy(_PERSONA_CONFIG),
        llm_client=client,
    )
    await agent.initialize_memory()
    return agent


def _channel_event() -> AgentEvent:
    """``AgentEvent`` shaped like ``ReceiveChannelMessage`` builds.

    This is the path that fails MT-COST-003 in PR 1 — the dispatcher's
    chat-error-recovery wrapper needs the :class:`AioRpcError` to
    propagate up, but the action loop currently swallows it.
    """
    return AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload={
            "content": "please answer",
            "channel_type": "dm",
            "mentions": ["stress-agent"],
            "respond_policy": "when_mentioned",
            "thread_parent_sender_id": "",
        },
        channel_id="dm:stress-agent:user-1",
        sender_id="user-1",
        message_id="msg-1",
    )


def _chat_event() -> AgentEvent:
    """``AgentEvent`` shaped like ``SendChatMessage`` builds.

    Discriminator is ``metadata["chat_session_id"]``; the SendChatMessage
    servicer arm of the gRPC interface also catches the propagated
    exception (PR 4).
    """
    return AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload={"content": "hello", "user_id": "user-1", "participant_type": "user"},
        sender_id="user-1",
        metadata={
            "chat_session_id": "chat-123",
            "sender_participant_type": "user",
        },
    )


def _tick_event() -> AgentEvent:
    return AgentEvent(event_type=EventType.TICK, payload={})


def _nonzero_injection() -> MemoryInjectionResult:
    return MemoryInjectionResult(memory_admitted_tokens=10)


# ─── RESOURCE_EXHAUSTED on chat/channel paths must propagate ────────────────


class TestResourceExhaustedPropagatesOnChannelPaths:
    """The action loop must let ``AioRpcError(RESOURCE_EXHAUSTED)`` escape on
    chat / channel events, so the dispatcher's chat-error-recovery wrapper
    (:func:`agents.chat_reply.dispatch_channel_event_with_chat_error_recovery`)
    can publish a structured-error reply on the originating channel.

    Without this propagation the orchestrator's REST chat waiter times
    out at HTTP 504 instead of returning HTTP 200 + ``reply_status="error"``
    (the MT-COST-003 surface contract).
    """

    @pytest.mark.asyncio
    async def test_channel_event_resource_exhausted_propagates(self) -> None:
        client = _make_client_raising(
            _rpc_error(
                grpc.StatusCode.RESOURCE_EXHAUSTED,
                details="agent already holds the maximum 3 active leases",
            ),
        )
        agent = await _make_agent(client)
        try:
            with patch.object(
                agent, "_inject_memory_context", return_value=_nonzero_injection(),
            ):
                with pytest.raises(grpc.aio.AioRpcError) as excinfo:
                    await agent.on_event(_channel_event())

            assert excinfo.value.code() == grpc.StatusCode.RESOURCE_EXHAUSTED, (
                "ISSUE-0066: AioRpcError(RESOURCE_EXHAUSTED) must propagate "
                "out of the action loop on channel events so the dispatcher's "
                "chat-error-recovery wrapper publishes the error reply."
            )
        finally:
            await agent.close_memory()

    @pytest.mark.asyncio
    async def test_chat_event_resource_exhausted_propagates(self) -> None:
        """``SendChatMessage`` servicer also catches the propagated error.

        Discriminated by ``metadata["chat_session_id"]`` (RFC 0016 OQ 9).
        Pre-fix the action loop's generic ``except Exception`` swallowed
        the gRPC error into ``COMPLETE_TASK("LLM provider error")`` — the
        chat handler then saw a fake-success reply rather than the
        structured ``RESOURCE_EXHAUSTED`` signal.
        """
        client = _make_client_raising(
            _rpc_error(grpc.StatusCode.RESOURCE_EXHAUSTED),
        )
        agent = await _make_agent(client)
        try:
            with patch.object(
                agent, "_inject_memory_context", return_value=_nonzero_injection(),
            ):
                with pytest.raises(grpc.aio.AioRpcError) as excinfo:
                    await agent.on_event(_chat_event())

            assert excinfo.value.code() == grpc.StatusCode.RESOURCE_EXHAUSTED
        finally:
            await agent.close_memory()


# ─── RESOURCE_EXHAUSTED on TICK must short-circuit to DO_NOTHING ────────────


class TestResourceExhaustedTickShortCircuit:
    """A TICK has no caller to render the denial to — re-raising would
    surface as ``Tick error`` in :meth:`TickScheduler._run` and the tick
    would simply be lost rather than reflected as idle. Symmetric to the
    existing :class:`BudgetExceededError` arm at action_loop.py line 391–422.
    """

    @pytest.mark.asyncio
    async def test_tick_resource_exhausted_returns_do_nothing(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        client = _make_client_raising(
            _rpc_error(
                grpc.StatusCode.RESOURCE_EXHAUSTED,
                details="agent already holds the maximum 3 active leases",
            ),
        )
        agent = await _make_agent(client)
        try:
            with patch.object(
                agent, "_inject_memory_context", return_value=_nonzero_injection(),
            ), patch.object(
                agent, "_has_active_goal_payload", return_value=True,
            ), caplog.at_level(
                logging.WARNING, logger="agents.persona_runtime.action_loop",
            ):
                actions = await agent.on_event(_tick_event())

            assert len(actions) == 1, (
                "ISSUE-0066: TICK with RESOURCE_EXHAUSTED must return exactly "
                f"one DO_NOTHING action (got {len(actions)})"
            )
            assert actions[0].action_type == ActionType.DO_NOTHING, (
                "ISSUE-0066: TICK with RESOURCE_EXHAUSTED must short-circuit "
                "to DO_NOTHING (symmetric to BudgetExceededError TICK arm), "
                f"got {actions[0].action_type!r}"
            )
            assert any(
                rec.levelno == logging.WARNING
                and (
                    "back-pressure" in rec.getMessage().lower()
                    or "resource" in rec.getMessage().lower()
                    or "lease" in rec.getMessage().lower()
                    or "capacity" in rec.getMessage().lower()
                    or "rate" in rec.getMessage().lower()
                )
                for rec in caplog.records
            ), (
                "ISSUE-0066: TICK short-circuit must emit a WARN log so "
                f"sustained back-pressure is visible to operators "
                f"(got logs: {[r.getMessage() for r in caplog.records]!r})"
            )
        finally:
            await agent.close_memory()

    @pytest.mark.asyncio
    async def test_tick_resource_exhausted_records_idle_reason(self) -> None:
        """The ``persona_tick_idle`` counter must carry an attribute that
        separates back-pressure-throttled idle from budget-throttled idle
        and from organic quiet periods (RFC 0023 §F counter contract,
        matching the dispatcher's ``error_reason`` vocabulary).
        """
        client = _make_client_raising(
            _rpc_error(grpc.StatusCode.RESOURCE_EXHAUSTED),
        )
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
                "agents.persona_runtime.llm_call_errors.try_get_instruments",
                return_value=inst,
            ):
                await agent.on_event(_tick_event())

            assert inst.persona_tick_idle.add.called, (
                "ISSUE-0066: RESOURCE_EXHAUSTED TICK must increment "
                "persona_tick_idle so dashboards see the throttle"
            )
            call = inst.persona_tick_idle.add.call_args
            assert call.args[0] == 1
            attrs = call.kwargs.get("attributes") or (
                call.args[1] if len(call.args) > 1 else {}
            )
            idle_reason = attrs.get("idle_reason")
            # Pin to ``resource_exhausted`` (matching the dispatcher's
            # ``error_reason`` vocabulary at
            # ``agents/chat_reply.py``). A fall-back to
            # ``budget_denied`` would still satisfy "separable from
            # empty_context_tick" but would silently mask a
            # RESOURCE_EXHAUSTED → BudgetExceededError relabel —
            # dashboards split on this attribute and lose the
            # back-pressure-vs-wallet-denial distinction if the
            # action loop maps both to one bucket.
            assert idle_reason == "resource_exhausted", (
                "ISSUE-0066: RESOURCE_EXHAUSTED TICK must record "
                "idle_reason='resource_exhausted' (matching the "
                "dispatcher's error_reason vocabulary); "
                f"got idle_reason={idle_reason!r}"
            )
            assert attrs.get("agent.id") == "stress-agent"
        finally:
            await agent.close_memory()


# ─── Other gRPC codes still fall through to the generic provider-error arm ──


class TestNonResourceExhaustedGrpcCodes:
    """Only ``RESOURCE_EXHAUSTED`` is back-pressure.  Other gRPC codes
    (``INTERNAL`` / ``UNAVAILABLE`` / ``INVALID_ARGUMENT``) indicate
    real provider-side problems and must still degrade to the
    ``COMPLETE_TASK("LLM provider error")`` generic surface so the
    chat handler / dispatcher renders a generic error reply rather
    than masking the failure as fake back-pressure.

    Same rationale as the dispatcher-layer
    ``test_resource_exhausted_other_grpc_codes_fall_through`` regression
    guard in
    [test_chat_path_resource_exhausted.py](test_chat_path_resource_exhausted.py).
    """

    @pytest.mark.asyncio
    async def test_internal_falls_through_to_complete_task(self) -> None:
        client = _make_client_raising(
            _rpc_error(grpc.StatusCode.INTERNAL, details="provider 500"),
        )
        agent = await _make_agent(client)
        try:
            with patch.object(
                agent, "_inject_memory_context", return_value=_nonzero_injection(),
            ):
                actions = await agent.on_event(_channel_event())

            assert len(actions) == 1
            assert actions[0].action_type == ActionType.COMPLETE_TASK, (
                "ISSUE-0066: non-RESOURCE_EXHAUSTED gRPC errors must still "
                "degrade to COMPLETE_TASK so they are not masked as "
                "back-pressure"
            )
            assert "LLM provider error" in actions[0].payload.get("result", "")
        finally:
            await agent.close_memory()

    @pytest.mark.asyncio
    async def test_unavailable_falls_through_to_complete_task(self) -> None:
        client = _make_client_raising(
            _rpc_error(grpc.StatusCode.UNAVAILABLE, details="provider down"),
        )
        agent = await _make_agent(client)
        try:
            with patch.object(
                agent, "_inject_memory_context", return_value=_nonzero_injection(),
            ):
                actions = await agent.on_event(_channel_event())

            assert actions[0].action_type == ActionType.COMPLETE_TASK
        finally:
            await agent.close_memory()


# ─── Non-gRPC exceptions still degrade to COMPLETE_TASK ─────────────────────


class TestGenericExceptionStillCaught:
    """A bare provider outage (network, 5xx through HTTP client, etc.)
    still degrades to ``COMPLETE_TASK`` exactly as in pre-fix v0.3.2.
    The narrow RESOURCE_EXHAUSTED arm must not regress this generic
    path or the dispatcher's
    ``test_generic_exception_does_not_publish_error_reply`` regression
    guard would no longer be honoured at the action-loop layer.
    """

    @pytest.mark.asyncio
    async def test_runtime_error_falls_through_to_complete_task(self) -> None:
        client = _make_client_raising(RuntimeError("provider 503"))
        agent = await _make_agent(client)
        try:
            with patch.object(
                agent, "_inject_memory_context", return_value=_nonzero_injection(),
            ):
                actions = await agent.on_event(_channel_event())

            assert len(actions) == 1
            assert actions[0].action_type == ActionType.COMPLETE_TASK
            assert "LLM provider error" in actions[0].payload.get("result", "")
        finally:
            await agent.close_memory()
