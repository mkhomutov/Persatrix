"""PR #248 deep-review follow-up tests on ``ReceiveChannelMessage``.

Split from ``test_receive_channel_message.py`` to keep both files under
the project's 500-line code-file cap. Shared fixtures (``_make_servicer``,
``_channel_event``) are intentionally re-declared here rather than
imported, so the file remains self-contained and matches the in-house
test-style conventions used elsewhere in the suite.

Covers:

- ``TestReceiveChannelMessageBackpressure`` — pinning discard-not-block
  backpressure. RFC 0024 Phase 4 moved in-flight backpressure from the
  servicer's ``_pending_dispatches`` cap (PR #248 deep review **Low**
  finding) onto the per-agent ``EventLoop``'s bounded queue.
- ``TestReceiveChannelMessageUnicodeBoundary`` — pinning that the
  4000-character content cap is measured in **codepoints**, not wire
  bytes (PR #248 deep review **NTH** finding).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import grpc

from agents.base import BaseAgent, TaskInput, TaskOutput, TaskStatus
from agents.dispatch import EventDispatcher
from agents.generated import task_pb2
from agents.server_servicers import AgentServiceServicer


class _StubAgent(BaseAgent):
    async def handle(self, task: TaskInput) -> TaskOutput:
        return TaskOutput(status=TaskStatus.COMPLETED, result="ok")


def _make_servicer() -> tuple[AgentServiceServicer, MagicMock]:
    agents: dict[str, BaseAgent] = {
        "ember-owl": _StubAgent(agent_id="ember-owl", config={"model": "test"}),
    }
    dispatcher = MagicMock(spec=EventDispatcher)
    # ``enqueue_inbound`` is a synchronous bool-returning method (accepted
    # / dropped) under the RFC 0024 Phase 4 fire-and-forget model.
    dispatcher.enqueue_inbound = MagicMock(return_value=True)
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
        # RFC 0011 PR 4b: validator requires ``respond_policy``.
        "respond_policy": "always",
        "thread_parent_sender_id": "",
    }
    fields.update(overrides)
    return task_pb2.ChannelMessageEvent(**fields)


# ─── Discard-not-block backpressure (RFC 0024 Decided §1) ──


class TestReceiveChannelMessageBackpressure:
    """RFC 0024 Phase 4 moved in-flight backpressure onto the EventLoop.

    The servicer's old ``_pending_dispatches`` cap (PR #248 deep review
    **Low**) is gone: the per-agent ``EventLoop``'s bounded queue now
    rejects wakes when full. ``EventDispatcher.enqueue_inbound`` returns
    ``False`` in that case and the handler surfaces ``TaskAck(success=
    False)`` — the orchestrator's existing per-ack failure path is the
    natural backpressure signal, symmetric with the validator's bounds.
    """

    async def test_rejects_when_event_loop_queue_full(self):
        servicer, dispatcher = _make_servicer()
        dispatcher.enqueue_inbound = MagicMock(return_value=False)
        ack = await servicer.ReceiveChannelMessage(
            _channel_event(), MagicMock(spec=grpc.aio.ServicerContext),
        )
        assert ack.success is False
        assert (
            "overload" in ack.error_message.lower()
            or "queue full" in ack.error_message.lower()
        )

    async def test_capacity_recovers_after_drain(self):
        """Once the loop drains (enqueue accepts again), requests succeed."""
        servicer, dispatcher = _make_servicer()
        dispatcher.enqueue_inbound = MagicMock(return_value=False)
        rejected = await servicer.ReceiveChannelMessage(
            _channel_event(message_id="msg-full"),
            MagicMock(spec=grpc.aio.ServicerContext),
        )
        assert rejected.success is False

        dispatcher.enqueue_inbound = MagicMock(return_value=True)
        accepted = await servicer.ReceiveChannelMessage(
            _channel_event(message_id="msg-after"),
            MagicMock(spec=grpc.aio.ServicerContext),
        )
        assert accepted.success is True


# ─── Unicode content boundary (PR #248 deep review NTH) ──


class TestReceiveChannelMessageUnicodeBoundary:
    """Pin the codepoint-vs-byte interpretation of the 4000-char cap.

    Proto strings are UTF-8 on the wire; ``proto3`` decodes them to
    Python ``str``, so ``len(request.content)`` measures **codepoints**,
    not bytes. The validator's documented cap is "4000 characters" — by
    the proto comment and by Python convention, that is 4000 codepoints.
    These tests pin that contract so a future "switch to bytes" refactor
    surfaces as a hard test failure rather than silently halving the
    effective limit for non-ASCII traffic.
    """

    async def test_accepts_4000_codepoints_of_multibyte(self):
        servicer, _ = _make_servicer()
        # 🦊 is U+1F98A — 4 UTF-8 bytes, 1 codepoint, 1 ``len(str)`` unit.
        # 4000 codepoints = 16 000 wire bytes; cap is codepoints, so accept.
        ack = await servicer.ReceiveChannelMessage(
            _channel_event(content="🦊" * 4000),
            MagicMock(spec=grpc.aio.ServicerContext),
        )
        assert ack.success is True

    async def test_rejects_4001_codepoints_of_multibyte(self):
        servicer, _ = _make_servicer()
        ack = await servicer.ReceiveChannelMessage(
            _channel_event(content="🦊" * 4001),
            MagicMock(spec=grpc.aio.ServicerContext),
        )
        assert ack.success is False
        assert "content" in ack.error_message
