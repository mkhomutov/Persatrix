"""RFC 0024 PR 3b — dispatch-site emission of the ``agent.wake.*`` counters.

:mod:`agents.tests.test_observability_metrics` proves the four wake
counters *register* and accept writes (via ``_touch_all``); it does not
prove the :class:`agents.event_loop.EventLoop` dispatch path actually
records them. This file closes that behavioural gap for the three counters
the substrate itself emits:

* ``agent.wake.inbound`` — recorded in ``_handle_wake`` when an
  :class:`InboundEventWake` drains.
* ``agent.wake.scheduled`` — recorded in ``_handle_wake`` when a
  :class:`ScheduledWake` drains (carries the ``timer_id``).
* ``agent.wake.dropped`` — recorded in ``EventLoop.enqueue`` when the
  queue is full and the wake is discarded (discard-not-block per RFC 0024
  Decided §1); must agree with :attr:`EventLoop.dropped_count`.

(``agent.wake.salience`` is covered end-to-end in
:mod:`agents.tests.test_event_loop_salience`.)

Without these tests a regression that drops an emission block would still
pass the registration/units inventory test and only surface in PR 4's
bored-persona cost-regression gate — far from the change that broke it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from typing import Any

import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from agents.event_loop import (
    EventLoop,
    InboundEventWake,
    ScheduledWake,
    SyncDispatchHandle,
)
from agents.observability import metrics as pmetrics
from agents.persona_types import ActionType, AgentAction, AgentEvent, EventType

_AGENT_ID = "wake-counter-persona"


@pytest.fixture
def metric_reader() -> Iterator[InMemoryMetricReader]:
    reader = InMemoryMetricReader()
    pmetrics.init_metrics(reader=reader)
    try:
        yield reader
    finally:
        asyncio.run(pmetrics.shutdown())


def _evt(payload: dict[str, Any] | None = None) -> AgentEvent:
    return AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload=payload or {"content": "hi"},
    )


def _build_loop(
    *,
    on_event: Callable[[AgentEvent], Awaitable[list[AgentAction]]] | None = None,
    on_tick: Callable[[ScheduledWake], Awaitable[None]] | None = None,
    queue_size: int = 1024,
) -> EventLoop:
    async def _default_event(_event: AgentEvent) -> list[AgentAction]:
        return [AgentAction(ActionType.DO_NOTHING, {})]

    async def _default_tick(_wake: ScheduledWake) -> None:
        return None

    return EventLoop(
        agent_id=_AGENT_ID,
        on_event=on_event or _default_event,
        on_tick=on_tick or _default_tick,
        queue_size=queue_size,
    )


def _counter_points(
    reader: InMemoryMetricReader, name: str,
) -> list[tuple[dict[str, Any], int]]:
    """Return ``[(attributes, value), …]`` for data points named ``name``."""
    data = reader.get_metrics_data()
    out: list[tuple[dict[str, Any], int]] = []
    if data is None:
        return out
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name != name:
                    continue
                for dp in m.data.data_points:  # type: ignore[union-attr]
                    out.append(
                        (dict(dp.attributes or {}), int(getattr(dp, "value", 0))),
                    )
    return out


class TestWakeCounterEmission:
    async def test_inbound_wake_increments_inbound_counter(
        self, metric_reader: InMemoryMetricReader,
    ) -> None:
        """Draining an ``InboundEventWake`` records ``agent.wake.inbound``."""
        loop = _build_loop()
        loop.start()
        try:
            handle = SyncDispatchHandle()
            # The counter is recorded in ``_handle_wake`` *before* ``on_event``
            # is awaited and the handle resolved, so once the await below
            # returns the data point is already in the SDK.
            loop.enqueue(InboundEventWake(event=_evt(), handle=handle))
            await asyncio.wait_for(handle, timeout=2.0)
        finally:
            await loop.stop(timeout=1.0)

        points = _counter_points(metric_reader, "agent.wake.inbound")
        assert sum(value for _, value in points) == 1
        assert all(attrs.get("wake.kind") == "inbound" for attrs, _ in points)
        assert all(attrs.get("agent.id") == _AGENT_ID for attrs, _ in points)

    async def test_scheduled_wake_increments_scheduled_counter_with_timer_id(
        self, metric_reader: InMemoryMetricReader,
    ) -> None:
        """Draining a ``ScheduledWake`` records ``agent.wake.scheduled`` with ``timer_id``."""
        ticked = asyncio.Event()

        async def _on_tick(_wake: ScheduledWake) -> None:
            ticked.set()

        loop = _build_loop(on_tick=_on_tick)
        loop.start()
        try:
            loop.enqueue(ScheduledWake(timer_id="legacy_tick", callback_kind="tick"))
            await asyncio.wait_for(ticked.wait(), timeout=2.0)
        finally:
            await loop.stop(timeout=1.0)

        points = _counter_points(metric_reader, "agent.wake.scheduled")
        assert sum(value for _, value in points) == 1
        assert any(
            attrs.get("wake.kind") == "scheduled"
            and attrs.get("timer_id") == "legacy_tick"
            for attrs, _ in points
        )

    async def test_dropped_wake_increments_dropped_counter(
        self, metric_reader: InMemoryMetricReader,
    ) -> None:
        """A queue-full discard records ``agent.wake.dropped`` and agrees with ``dropped_count``."""
        gate = asyncio.Event()

        async def _on_event(_event: AgentEvent) -> list[AgentAction]:
            await gate.wait()
            return [AgentAction(ActionType.DO_NOTHING, {})]

        loop = _build_loop(on_event=_on_event, queue_size=2)
        loop.start()
        try:
            # First wake is dequeued and blocks on the gate; the next two fill
            # the size-2 queue, and the fourth overflows → discarded.
            assert loop.enqueue(InboundEventWake(event=_evt(), handle=None))
            await asyncio.sleep(0.05)
            assert loop.enqueue(InboundEventWake(event=_evt(), handle=None))
            assert loop.enqueue(InboundEventWake(event=_evt(), handle=None))
            assert not loop.enqueue(InboundEventWake(event=_evt(), handle=None))
            assert loop.dropped_count == 1
        finally:
            gate.set()
            await loop.stop(timeout=1.0)

        points = _counter_points(metric_reader, "agent.wake.dropped")
        assert sum(value for _, value in points) == 1
        assert all(attrs.get("wake.kind") == "dropped" for attrs, _ in points)
