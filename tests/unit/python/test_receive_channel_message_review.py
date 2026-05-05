"""PR #248 deep-review follow-up tests on ``ReceiveChannelMessage``.

Split from ``test_receive_channel_message.py`` to keep both files under
the project's 500-line code-file cap. Shared fixtures (``_make_servicer``,
``_channel_event``, ``_drain``) are intentionally re-declared here rather
than imported, so the file remains self-contained and matches the
in-house test-style conventions used elsewhere in the suite.

Covers:

- ``TestReceiveChannelMessageBackpressure`` — pinning the bounded
  ``_pending_dispatches`` cap (PR #248 deep review **Low** finding).
- ``TestReceiveChannelMessageUnicodeBoundary`` — pinning that the
  4000-character content cap is measured in **codepoints**, not wire
  bytes (PR #248 deep review **NTH** finding).
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import grpc

from agents.base import BaseAgent, TaskInput, TaskOutput, TaskStatus
from agents.dispatch import EventDispatcher
from agents.generated import task_pb2
from agents.server_servicers import AgentServiceServicer


class _StubAgent(BaseAgent):
    async def handle(self, task: TaskInput) -> TaskOutput:
        return TaskOutput(status=TaskStatus.COMPLETED, result="ok")


def _make_servicer() -> tuple[AgentServiceServicer, MagicMock]:
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
    pending = getattr(servicer, "_pending_dispatches", None)
    if pending:
        await asyncio.gather(*list(pending), return_exceptions=True)


# ─── Pending-dispatch backpressure (PR #248 deep review Low) ──


class TestReceiveChannelMessageBackpressure:
    """``_pending_dispatches`` was unbounded prior to this fix.

    A stalled :meth:`EventDispatcher.dispatch` plus a chatty publisher
    on the cleartext gRPC port would grow the set without bound — a
    slow-burn DoS surface symmetric with the validator's other bounds
    work. The cap returns ``TaskAck(success=False)`` once the in-flight
    queue is full so the orchestrator's existing per-ack failure path
    becomes the natural backpressure signal.
    """

    async def test_rejects_when_pending_queue_full(self):
        from agents.server_servicers import _MAX_PENDING_DISPATCHES

        servicer, dispatcher = _make_servicer()
        gate = asyncio.Event()

        async def stalled_dispatch(*_args: Any, **_kwargs: Any) -> list[Any]:
            await gate.wait()
            return []

        dispatcher.dispatch = AsyncMock(side_effect=stalled_dispatch)
        ctx = MagicMock(spec=grpc.aio.ServicerContext)

        for i in range(_MAX_PENDING_DISPATCHES):
            ack = await servicer.ReceiveChannelMessage(
                _channel_event(message_id=f"msg-{i:04d}"), ctx
            )
            assert ack.success is True, f"unexpected reject at i={i}"
        assert len(servicer._pending_dispatches) == _MAX_PENDING_DISPATCHES

        ack = await servicer.ReceiveChannelMessage(
            _channel_event(message_id="msg-overflow"), ctx
        )
        assert ack.success is False
        assert (
            "overload" in ack.error_message.lower()
            or "pending" in ack.error_message.lower()
        )
        # Strong-ref set length is the load-bearing observable: the
        # overflow ack did not enqueue a new task. (We deliberately do
        # NOT assert ``dispatcher.dispatch.call_count`` here — the
        # ``create_task``-scheduled coroutines do not run until the
        # event loop yields, and ``ReceiveChannelMessage`` returns
        # synchronously.)
        assert len(servicer._pending_dispatches) == _MAX_PENDING_DISPATCHES

        gate.set()
        await _drain(servicer)
        assert len(servicer._pending_dispatches) == 0

    async def test_capacity_recovers_after_drain(self):
        """A request that arrives after the queue drains MUST succeed."""
        from agents.server_servicers import _MAX_PENDING_DISPATCHES

        servicer, dispatcher = _make_servicer()
        gate = asyncio.Event()

        async def stalled_dispatch(*_args: Any, **_kwargs: Any) -> list[Any]:
            await gate.wait()
            return []

        dispatcher.dispatch = AsyncMock(side_effect=stalled_dispatch)
        ctx = MagicMock(spec=grpc.aio.ServicerContext)

        for i in range(_MAX_PENDING_DISPATCHES):
            await servicer.ReceiveChannelMessage(
                _channel_event(message_id=f"msg-{i:04d}"), ctx
            )

        gate.set()
        await _drain(servicer)
        assert len(servicer._pending_dispatches) == 0

        gate2 = asyncio.Event()

        async def stalled2(*_args: Any, **_kwargs: Any) -> list[Any]:
            await gate2.wait()
            return []

        dispatcher.dispatch = AsyncMock(side_effect=stalled2)
        ack = await servicer.ReceiveChannelMessage(
            _channel_event(message_id="msg-after"), ctx
        )
        assert ack.success is True
        gate2.set()
        await _drain(servicer)


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
        await _drain(servicer)
        assert ack.success is True

    async def test_rejects_4001_codepoints_of_multibyte(self):
        servicer, _ = _make_servicer()
        ack = await servicer.ReceiveChannelMessage(
            _channel_event(content="🦊" * 4001),
            MagicMock(spec=grpc.aio.ServicerContext),
        )
        await _drain(servicer)
        assert ack.success is False
        assert "content" in ack.error_message
