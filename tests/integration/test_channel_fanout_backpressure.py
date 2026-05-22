"""RFC 0024 Phase 4 — channel-message fan-out backpressure.

Channel-message origin under load is the first place where a slow agent
can plausibly hit the per-agent :class:`~agents.event_loop.EventLoop`
queue cap ([RFC 0024 §F](
../../docs/rfcs/0024-event-driven-scheduling.md#f-failure-modes), Decided
§1: **discard, not block**). This covers the branch the bored-persona gate
deliberately does not exercise — *capped* wakes rather than *zero* wakes.

The test drives the real channel path (``ReceiveChannelMessage`` →
``EventDispatcher.enqueue_inbound`` → ``EventLoop.enqueue``) against a loop
whose ``on_inbound`` handler is parked on a gate, so the supervisor cannot
drain. Once the bounded queue is full, the next enqueue is **discarded**:

* the producer (``ReceiveChannelMessage``) never blocks — it returns a
  ``TaskAck`` synchronously for every message, and
* the overflow surfaces as ``TaskAck(success=False)`` and increments the
  ``agent.wake.dropped`` counter, which must agree with
  :attr:`EventLoop.dropped_count`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Iterator
from typing import Any
from unittest.mock import MagicMock

import grpc
import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from agents.base import BaseAgent, TaskInput, TaskOutput, TaskStatus
from agents.dispatch import EventDispatcher
from agents.event_loop import EventLoop, ScheduledWake
from agents.generated import task_pb2
from agents.observability import metrics as pmetrics
from agents.persona_types import ActionType, AgentAction, AgentEvent
from agents.server_servicers import AgentServiceServicer
from agents.tick import TickScheduler

_AGENT_ID = "fanout-persona"
_QUEUE_SIZE = 2


class _StubAgent(BaseAgent):
    async def handle(self, task: TaskInput) -> TaskOutput:
        return TaskOutput(status=TaskStatus.COMPLETED, result="ok")


@pytest.fixture
def metric_reader() -> Iterator[InMemoryMetricReader]:
    reader = InMemoryMetricReader()
    pmetrics.init_metrics(reader=reader)
    try:
        yield reader
    finally:
        asyncio.run(pmetrics.shutdown())


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
        "respond_policy": "always",
        "thread_parent_sender_id": "",
    }
    fields.update(overrides)
    return task_pb2.ChannelMessageEvent(**fields)


def _dropped_total(reader: InMemoryMetricReader) -> int:
    data = reader.get_metrics_data()
    if data is None:
        return 0
    total = 0
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name != "agent.wake.dropped":
                    continue
                for dp in m.data.data_points:  # type: ignore[union-attr]
                    total += int(getattr(dp, "value", 0))
    return total


async def test_channel_queue_full_discards_and_counts(
    metric_reader: InMemoryMetricReader,
) -> None:
    gate = asyncio.Event()

    async def _blocked_inbound(_event: AgentEvent) -> None:
        # Park the supervisor on the first wake so the bounded queue fills.
        await gate.wait()

    async def _noop_event(_event: AgentEvent) -> list[AgentAction]:
        return [AgentAction(ActionType.DO_NOTHING, {})]

    async def _noop_tick(_wake: ScheduledWake) -> Awaitable[None] | None:
        return None

    loop = EventLoop(
        agent_id=_AGENT_ID,
        on_event=_noop_event,
        on_tick=_noop_tick,  # type: ignore[arg-type]
        on_inbound=_blocked_inbound,
        queue_size=_QUEUE_SIZE,
    )
    loop.start()

    # A minimal scheduler stand-in: ``enqueue_inbound`` only reads
    # ``is_running`` and ``event_loop``. Spec'd to TickScheduler so the
    # attribute surface matches production.
    scheduler = MagicMock(spec=TickScheduler)
    scheduler.is_running = True
    scheduler.event_loop = loop

    dispatcher = EventDispatcher()
    dispatcher.register_tick_scheduler(_AGENT_ID, scheduler)  # type: ignore[arg-type]
    servicer = AgentServiceServicer(
        {_AGENT_ID: _StubAgent(agent_id=_AGENT_ID, config={"model": "test"})},
        dispatcher,
    )
    ctx = MagicMock(spec=grpc.aio.ServicerContext)

    try:
        # msg-1 drains into the parked ``on_inbound`` and blocks there.
        ack1 = await servicer.ReceiveChannelMessage(
            _channel_event(message_id="msg-1"), ctx,
        )
        assert ack1.success is True
        # Let the supervisor dequeue msg-1 and park on the gate so the
        # queue is empty again before we fill it.
        await asyncio.sleep(0.05)

        # msg-2 / msg-3 fill the size-2 queue; both accepted.
        ack2 = await servicer.ReceiveChannelMessage(
            _channel_event(message_id="msg-2"), ctx,
        )
        ack3 = await servicer.ReceiveChannelMessage(
            _channel_event(message_id="msg-3"), ctx,
        )
        assert ack2.success is True
        assert ack3.success is True

        # msg-4 overflows → discarded, not blocked. The producer returns
        # immediately with a taxonomised overload reason.
        ack4 = await servicer.ReceiveChannelMessage(
            _channel_event(message_id="msg-4"), ctx,
        )
        assert ack4.success is False
        assert (
            "overload" in ack4.error_message.lower()
            or "queue full" in ack4.error_message.lower()
        )

        assert loop.dropped_count == 1
    finally:
        gate.set()
        await loop.stop(timeout=1.0)

    # The substrate counter must agree with the in-memory drop count.
    assert _dropped_total(metric_reader) == 1
