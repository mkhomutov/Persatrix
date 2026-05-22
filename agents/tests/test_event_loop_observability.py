"""Reentrant-resolver observability-parity tests (RFC 0024 PR 5 re-review).

Pins that the detached ``_spawn_reentrant`` resolver matches the supervised
FIFO path's observability surface:

* it records the ``agent.wake.inbound`` OTEL counter for a reentrant inbound
  wake (which never reaches ``_handle_wake``), and
* it logs ``agent.event_loop.wake_failed`` with a traceback when ``on_event``
  raises, before rejecting the handle.

Split out of ``test_event_loop_lifecycle.py`` for file-size review-
friendliness (RFC 0024 PR 5 — the lifecycle file exceeded the 500-line
limit once the in-flight-handle hard-cancel test landed). These are the
OTEL-fixture-bearing tests; the lifecycle file keeps the pure-correctness
contracts.

The ``_evt`` / ``_build_loop`` helpers are duplicated from
``test_event_loop_lifecycle.py`` rather than shared — pytest does not
cross-import sibling test modules, and the helpers are small enough that
duplication is cheaper than threading a shared ``conftest.py`` fixture
(the precedent the now-removed ``test_event_loop_deferred.py`` set).
"""

from __future__ import annotations

import asyncio
import logging
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
from agents.persona_types import (
    ActionType,
    AgentAction,
    AgentEvent,
    EventType,
)


@pytest.fixture
def metric_reader() -> Iterator[InMemoryMetricReader]:
    """An in-memory OTEL reader wired into the process-global instruments.

    Mirrors the fixture in ``test_observability_metrics.py`` — the
    EventLoop records the ``agent.wake.*`` counters via the module-global
    instruments, so a test that asserts on them must initialise the
    metrics provider and tear it down afterwards.
    """
    reader = InMemoryMetricReader()
    pmetrics.init_metrics(reader=reader)
    try:
        yield reader
    finally:
        asyncio.run(pmetrics.shutdown())


def _collect(reader: InMemoryMetricReader) -> dict[str, Any]:
    data = reader.get_metrics_data()
    out: dict[str, Any] = {}
    if data is None:
        return out
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                out[m.name] = m
    return out


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
    agent_id: str = "test-agent",
) -> EventLoop:
    async def _default_event(event: AgentEvent) -> list[AgentAction]:
        return [AgentAction(ActionType.DO_NOTHING, {})]

    async def _default_tick(wake: ScheduledWake) -> None:
        return None

    return EventLoop(
        agent_id=agent_id,
        on_event=on_event or _default_event,
        on_tick=on_tick or _default_tick,
        queue_size=queue_size,
    )


class TestReentrantDispatchObservability:
    """RFC 0024 PR 5 re-review (review finding #1) — the reentrant resolver
    must have observability parity with the supervised FIFO path.

    The FIFO path records the ``agent.wake.inbound`` counter for every
    inbound wake and logs ``agent.event_loop.wake_failed`` (with a
    traceback) when ``on_event`` raises.  The reentrant escape hatch runs
    ``on_event`` on a detached resolver task that bypasses both, so a
    reentrant inbound wake was invisible to the inbound counter and a
    reentrant failure was rejected silently.  These tests pin the parity.
    """

    async def test_reentrant_inbound_wake_records_inbound_metric(
        self, metric_reader: InMemoryMetricReader,
    ):
        """A reentrant inbound wake increments ``agent.wake.inbound`` just
        like a FIFO-drained one.  The reentrant wake is triggered from a
        ``ScheduledWake`` (which records ``agent.wake.scheduled``, not
        inbound), so the *only* contributor to this agent's inbound counter
        is the reentrant resolver — isolating the behaviour under test.
        """
        done = asyncio.Event()
        loop_ref: list[EventLoop] = []

        async def _on_tick(wake: ScheduledWake) -> None:
            # Reentrant handle-bearing inbound enqueue from inside on_tick.
            handle = SyncDispatchHandle()
            loop_ref[0].enqueue(
                InboundEventWake(event=_evt({"level": "inner"}), handle=handle),
            )
            await handle
            done.set()

        loop = _build_loop(on_tick=_on_tick, agent_id="reentrant-metric")
        loop_ref.append(loop)
        loop.start()
        try:
            loop.enqueue(ScheduledWake(timer_id="t", callback_kind="tick"))
            await asyncio.wait_for(done.wait(), timeout=2.0)
        finally:
            await loop.stop(timeout=1.0)

        m = _collect(metric_reader).get("agent.wake.inbound")
        assert m is not None, (
            "reentrant inbound wake must record agent.wake.inbound"
        )
        total = sum(
            getattr(dp, "value", 0)
            for dp in m.data.data_points
            if dict(dp.attributes).get("agent.id") == "reentrant-metric"
        )
        assert total == 1

    async def test_reentrant_on_event_failure_logs_and_rejects(self, caplog):
        """A reentrant resolver whose ``on_event`` raises rejects the handle
        *and* logs ``wake_failed`` with a traceback — parity with the
        supervised FIFO path.  The outer body swallows the rejection so the
        only ``wake_failed`` record can come from the reentrant resolver.
        """
        boom = RuntimeError("reentrant boom")
        inner_handle_ref: list[SyncDispatchHandle] = []
        loop_ref: list[EventLoop] = []

        async def _on_event(event: AgentEvent) -> list[AgentAction]:
            if event.payload.get("level") == "outer":
                inner_handle = SyncDispatchHandle()
                inner_handle_ref.append(inner_handle)
                loop_ref[0].enqueue(
                    InboundEventWake(
                        event=_evt({"level": "inner"}), handle=inner_handle,
                    ),
                )
                try:
                    return await inner_handle
                except RuntimeError:
                    # Swallow so the supervised path does not also log a
                    # wake_failed for the outer wake — isolates the resolver.
                    return [AgentAction(ActionType.DO_NOTHING, {})]
            raise boom

        loop = _build_loop(on_event=_on_event)
        loop_ref.append(loop)
        loop.start()
        try:
            outer_handle = SyncDispatchHandle()
            with caplog.at_level(logging.ERROR, logger="agents.event_loop"):
                loop.enqueue(
                    InboundEventWake(
                        event=_evt({"level": "outer"}), handle=outer_handle,
                    ),
                )
                # Outer resolves with the swallowed result once the inner
                # rejection has propagated through the resolver.
                assert await asyncio.wait_for(outer_handle, timeout=0.5) == [
                    AgentAction(ActionType.DO_NOTHING, {}),
                ]
            # Resolver rejected the inner handle...
            assert inner_handle_ref[0].done()
            with pytest.raises(RuntimeError, match="reentrant boom"):
                await inner_handle_ref[0]
            # ...and logged the failure with a traceback (parity with the
            # supervised FIFO path). Without the fix the rejection is silent.
            failed = [
                r for r in caplog.records if "wake_failed" in r.getMessage()
            ]
            assert len(failed) == 1
            assert failed[0].exc_info is not None
        finally:
            await loop.stop(timeout=1.0)
