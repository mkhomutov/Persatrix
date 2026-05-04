"""
Tests for the AgentService.ReceiveChannelMessage stub.

RFC 0011 PR 3 wires the proto + RPC surface only; the real handler
(AgentEvent(CHANNEL_MESSAGE) → EventDispatcher) lands in PR 4.

Until then the stub MUST signal ``success=False`` so the orchestrator-side
dispatcher (when it lands, possibly independently of the real handler) does
not interpret the response as a successful delivery and silently drop the
message. See PR #246 deep review, finding H1 ("Stub silently acks every
event"): returning ``TaskAck(success=True)`` would be indistinguishable from
a real ack on the wire and create a black-hole window between PR 3 and PR 4.

These tests pin three things to prevent regression on the next ``make proto``
regen / hand-edit cycle:

1. The method exists on AgentServiceServicer and accepts a
   ``ChannelMessageEvent`` (catches accidental method removal — the deleted
   ``ChannelService.SendMessage`` test file's removal motivated this).
2. The stub returns ``TaskAck(success=False)`` (NOT ``True``) — pins the H1
   contract.
3. ``error_message`` references RFC 0011 PR 4 so operators reading the wire
   trace can locate the tracking work.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import grpc

from agents.base import BaseAgent, TaskInput, TaskOutput, TaskStatus
from agents.dispatch import EventDispatcher
from agents.generated import task_pb2
from agents.server_servicers import AgentServiceServicer


class _StubAgent(BaseAgent):
    async def handle(self, task: TaskInput) -> TaskOutput:
        return TaskOutput(status=TaskStatus.COMPLETED, result="ok")


def _make_servicer() -> AgentServiceServicer:
    agent = _StubAgent(agent_id="ember-owl", config={"model": "test"})
    dispatcher = MagicMock(spec=EventDispatcher)
    return AgentServiceServicer({"ember-owl": agent}, dispatcher)


def _channel_event() -> task_pb2.ChannelMessageEvent:
    return task_pb2.ChannelMessageEvent(
        message_id="msg-001",
        channel_id="group:general",
        channel_type="group",
        sender_id="local",
        content="hello",
        timestamp="2026-05-04T00:00:00Z",
        thread_id="",
        mentions=[],
    )


class TestReceiveChannelMessageStub:
    async def test_returns_task_ack(self):
        """Method exists on the servicer and accepts a ChannelMessageEvent."""
        servicer = _make_servicer()
        context = MagicMock(spec=grpc.aio.ServicerContext)

        ack = await servicer.ReceiveChannelMessage(_channel_event(), context)

        assert isinstance(ack, task_pb2.TaskAck)

    async def test_signals_unimplemented_via_success_false(self):
        """Stub MUST NOT silently ack — see PR #246 review H1."""
        servicer = _make_servicer()
        context = MagicMock(spec=grpc.aio.ServicerContext)

        ack = await servicer.ReceiveChannelMessage(_channel_event(), context)

        assert ack.success is False, (
            "Stub returned success=True; this would silently drop every "
            "channel message until RFC 0011 PR 4 lands the real handler. "
            "See PR #246 deep review finding H1."
        )

    async def test_error_message_points_at_rfc_0011_pr_4(self):
        """Operators reading wire traces must be able to locate the tracking work."""
        servicer = _make_servicer()
        context = MagicMock(spec=grpc.aio.ServicerContext)

        ack = await servicer.ReceiveChannelMessage(_channel_event(), context)

        assert ack.error_message, "error_message must be populated when success=False"
        assert "PR 4" in ack.error_message or "0011" in ack.error_message
