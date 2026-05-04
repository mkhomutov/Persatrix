"""
Tests for the AgentService.ReceiveChannelMessage handler (RFC 0011 PR 4a).

PR 3 (#246) shipped the proto + RPC surface with a ``TaskAck(success=False)``
stub. PR 4a (this slice) replaces the stub with a real handler that:

1. Validates the wire-side ``ChannelMessageEvent`` (mentions cap, content
   size, channel_type/channel_id prefix agreement, thread_id length).
2. Resolves the target agent on this single-agent-per-process server
   (``agents/server.py`` enforces single-agent today; multi-agent
   disambiguation deferred — see ``error_message`` taxonomy below).
3. Constructs an ``AgentEvent(event_type=CHANNEL_MESSAGE)`` populated
   from the proto fields plus the additive top-level ``thread_id``
   per RFC 0011 §D.
4. Schedules dispatch via ``asyncio.create_task`` with a strong-ref task
   set (``self._pending_dispatches``) per PR #246 deep review Should-Fix
   #2 — Python 3.11+ GCs weakly-held tasks mid-flight.
5. Returns ``TaskAck(success=True)`` on enqueue (at-most-once contract;
   the response gate / executor lands in PR 4b).

Validation failures return ``TaskAck(success=False, error_message=...)``
and DO NOT enqueue; ``error_message`` is taxonomised so operators reading
wire traces can locate the failure class.

Hard renames of ``EventType.MESSAGE_RECEIVED`` → ``CHANNEL_MESSAGE`` and
``ActionType.SEND_MESSAGE`` → ``SEND_CHANNEL_MESSAGE`` are deferred to a
follow-up PR that lands atomically with the chat-path migration per the
RFC 0011 amendment (chat is the heavy producer of the old names; renaming
without migrating chat would leave ``main`` broken). This PR adds the new
enum members additively and keeps the old names intact.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import grpc

import pytest

from agents.base import BaseAgent, TaskInput, TaskOutput, TaskStatus
from agents.dispatch import EventDispatcher
from agents.generated import task_pb2
from agents.persona_types import AgentEvent, EventType
from agents.server_servicers import AgentServiceServicer


class _StubAgent(BaseAgent):
    async def handle(self, task: TaskInput) -> TaskOutput:
        return TaskOutput(status=TaskStatus.COMPLETED, result="ok")


def _make_servicer(
    *,
    agents: dict[str, BaseAgent] | None = None,
) -> tuple[AgentServiceServicer, MagicMock]:
    if agents is None:
        agents = {"ember-owl": _StubAgent(agent_id="ember-owl", config={"model": "test"})}
    dispatcher = MagicMock(spec=EventDispatcher)
    dispatcher.dispatch = AsyncMock(return_value=[])
    return AgentServiceServicer(agents, dispatcher), dispatcher


def _channel_event(**overrides: Any) -> task_pb2.ChannelMessageEvent:
    fields: dict[str, Any] = {
        "message_id": "msg-001",
        "channel_id": "group:general",
        "channel_type": "group",
        "sender_id": "iron-fox",
        "content": "hello",
        "timestamp": "2026-05-04T00:00:00Z",
        "thread_id": "",
        "mentions": [],
    }
    fields.update(overrides)
    return task_pb2.ChannelMessageEvent(**fields)


async def _drain(servicer: AgentServiceServicer) -> None:
    """Wait for any fire-and-forget dispatch tasks to complete."""
    pending = getattr(servicer, "_pending_dispatches", None)
    if pending:
        # Snapshot — the done_callback mutates the set as tasks finish.
        await asyncio.gather(*list(pending), return_exceptions=True)


# ─── Happy path ────────────────────────────────────────────


class TestReceiveChannelMessageHappyPath:
    async def test_returns_success_true(self):
        servicer, _ = _make_servicer()
        ack = await servicer.ReceiveChannelMessage(
            _channel_event(), MagicMock(spec=grpc.aio.ServicerContext)
        )
        await _drain(servicer)
        assert isinstance(ack, task_pb2.TaskAck)
        assert ack.success is True
        assert ack.error_message == ""

    async def test_dispatches_channel_message_event(self):
        servicer, dispatcher = _make_servicer()
        await servicer.ReceiveChannelMessage(
            _channel_event(
                channel_id="dm:alpha:beta",
                channel_type="dm",
                sender_id="alpha",
                content="ping",
                thread_id="msg-parent-123",
                mentions=["ember-owl", "iron-fox"],
            ),
            MagicMock(spec=grpc.aio.ServicerContext),
        )
        await _drain(servicer)

        dispatcher.dispatch.assert_awaited_once()
        call = dispatcher.dispatch.await_args
        assert call.args[0] == "ember-owl"
        event = call.args[1]
        assert isinstance(event, AgentEvent)
        assert event.event_type is EventType.CHANNEL_MESSAGE
        assert event.channel_id == "dm:alpha:beta"
        assert event.sender_id == "alpha"
        assert event.message_id == "msg-001"
        assert event.thread_id == "msg-parent-123"
        assert event.payload["content"] == "ping"
        assert event.payload["channel_type"] == "dm"
        assert event.payload["mentions"] == ["ember-owl", "iron-fox"]


# ─── Validation: invalid inputs are rejected without dispatch ──


class TestReceiveChannelMessageValidation:
    async def test_rejects_oversized_content(self):
        servicer, dispatcher = _make_servicer()
        ack = await servicer.ReceiveChannelMessage(
            _channel_event(content="x" * 4001),
            MagicMock(spec=grpc.aio.ServicerContext),
        )
        await _drain(servicer)
        assert ack.success is False
        assert "content" in ack.error_message
        dispatcher.dispatch.assert_not_awaited()

    async def test_rejects_too_many_mentions(self):
        servicer, dispatcher = _make_servicer()
        ack = await servicer.ReceiveChannelMessage(
            _channel_event(mentions=[f"agent-{i}" for i in range(11)]),
            MagicMock(spec=grpc.aio.ServicerContext),
        )
        await _drain(servicer)
        assert ack.success is False
        assert "mentions" in ack.error_message
        dispatcher.dispatch.assert_not_awaited()

    async def test_rejects_invalid_mention_id(self):
        servicer, dispatcher = _make_servicer()
        ack = await servicer.ReceiveChannelMessage(
            _channel_event(mentions=["BAD_ID"]),
            MagicMock(spec=grpc.aio.ServicerContext),
        )
        await _drain(servicer)
        assert ack.success is False
        assert "mention" in ack.error_message
        dispatcher.dispatch.assert_not_awaited()

    async def test_rejects_oversized_thread_id(self):
        servicer, dispatcher = _make_servicer()
        ack = await servicer.ReceiveChannelMessage(
            _channel_event(thread_id="x" * 129),
            MagicMock(spec=grpc.aio.ServicerContext),
        )
        await _drain(servicer)
        assert ack.success is False
        assert "thread_id" in ack.error_message
        dispatcher.dispatch.assert_not_awaited()

    @pytest.mark.parametrize(
        ("channel_id", "channel_type"),
        [
            ("group:general", "dm"),       # type disagrees with prefix
            ("dm:a:b", "group"),
            ("thread:x", "group"),
            ("noprefix", "group"),         # unknown prefix
            ("group:general", "bogus"),    # unknown type
        ],
    )
    async def test_rejects_channel_type_disagreement(
        self, channel_id: str, channel_type: str
    ):
        servicer, dispatcher = _make_servicer()
        ack = await servicer.ReceiveChannelMessage(
            _channel_event(channel_id=channel_id, channel_type=channel_type),
            MagicMock(spec=grpc.aio.ServicerContext),
        )
        await _drain(servicer)
        assert ack.success is False
        assert "channel_type" in ack.error_message or "channel_id" in ack.error_message
        dispatcher.dispatch.assert_not_awaited()


# ─── Agent resolution ──────────────────────────────────────


class TestReceiveChannelMessageAgentResolution:
    async def test_no_agents_registered_returns_failure(self):
        servicer, dispatcher = _make_servicer(agents={})
        ack = await servicer.ReceiveChannelMessage(
            _channel_event(), MagicMock(spec=grpc.aio.ServicerContext)
        )
        await _drain(servicer)
        assert ack.success is False
        assert "no agents" in ack.error_message.lower()
        dispatcher.dispatch.assert_not_awaited()

    async def test_multi_agent_server_returns_failure(self):
        # v0.3.0 server is single-agent-per-process; multi-agent
        # disambiguation requires an additive `recipient_id` proto field
        # deferred to a follow-up PR (chat-path migration scope).
        agents = {
            "ember-owl": _StubAgent(agent_id="ember-owl", config={"model": "test"}),
            "iron-fox": _StubAgent(agent_id="iron-fox", config={"model": "test"}),
        }
        servicer, dispatcher = _make_servicer(agents=agents)
        ack = await servicer.ReceiveChannelMessage(
            _channel_event(), MagicMock(spec=grpc.aio.ServicerContext)
        )
        await _drain(servicer)
        assert ack.success is False
        assert "multi" in ack.error_message.lower() or "recipient" in ack.error_message.lower()
        dispatcher.dispatch.assert_not_awaited()


# ─── Strong-ref pattern (PR #246 Should-Fix #2) ────────────


class TestReceiveChannelMessageStrongRef:
    async def test_pending_dispatch_strongly_referenced(self):
        """Fire-and-forget task must be held in a strong-ref set until done."""
        servicer, dispatcher = _make_servicer()

        # Block the dispatch so the task is observable mid-flight.
        gate = asyncio.Event()

        async def slow_dispatch(*_args: Any, **_kwargs: Any) -> list[Any]:
            await gate.wait()
            return []

        dispatcher.dispatch = AsyncMock(side_effect=slow_dispatch)

        ack = await servicer.ReceiveChannelMessage(
            _channel_event(), MagicMock(spec=grpc.aio.ServicerContext)
        )
        assert ack.success is True

        # While the dispatch is parked in `slow_dispatch`, the task must be
        # held in the servicer's strong-ref set.
        pending = getattr(servicer, "_pending_dispatches")
        assert len(pending) == 1
        task = next(iter(pending))
        assert not task.done()

        # Release and verify the done_callback discards from the set.
        gate.set()
        await asyncio.wait_for(task, timeout=1.0)
        assert len(pending) == 0
