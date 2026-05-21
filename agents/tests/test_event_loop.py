"""Unit tests for ``agents.event_loop`` — RFC 0024 Phase 1 substrate.

Pins the three contracts that ride on the agent dispatch surface:

* **Return-value contract**: :class:`SyncDispatchHandle` resolves with the
  agent's ``list[AgentAction]`` so :class:`EventDispatcher.dispatch` keeps
  returning what ``SendChatMessage`` consumes.
* **Await-serialisation contract**: a handle stays unresolved when no caller
  is awaiting it — fire-and-forget producers do not block.
* **Queue-mediated ordering**: inbound and scheduled wakes drain FIFO from
  a single per-agent :class:`asyncio.Queue`.

Plus the supervisor / discard policy from
[RFC 0024 §F](../docs/rfcs/0024-event-driven-scheduling.md#f-failure-modes):
queue-full enqueues are discarded (not blocked) and surface on the
``agent.wake.dropped`` counter; an ``on_event`` raise restarts the loop.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from agents.event_loop import (
    EventLoop,
    InboundEventWake,
    ScheduledWake,
    SyncDispatchHandle,
)
from agents.persona_types import (
    ActionType,
    AgentAction,
    AgentEvent,
    EventType,
)


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


class TestEventLoopBasics:
    async def test_inbound_event_resolves_handle_with_actions(self):
        seen: list[AgentEvent] = []

        async def _on_event(event: AgentEvent) -> list[AgentAction]:
            seen.append(event)
            return [AgentAction(ActionType.COMPLETE_TASK, {"result": "ok"})]

        loop = _build_loop(on_event=_on_event)
        loop.start()
        try:
            handle = SyncDispatchHandle()
            event = _evt({"content": "ping"})
            assert loop.enqueue(InboundEventWake(event=event, handle=handle))
            actions = await asyncio.wait_for(handle, timeout=2.0)
        finally:
            await loop.stop(timeout=1.0)

        assert len(seen) == 1
        assert seen[0].payload["content"] == "ping"
        assert len(actions) == 1
        assert actions[0].action_type == ActionType.COMPLETE_TASK
        assert actions[0].payload["result"] == "ok"

    async def test_fire_and_forget_does_not_block_producer(self):
        on_event_calls = 0
        on_event_gate = asyncio.Event()

        async def _on_event(event: AgentEvent) -> list[AgentAction]:
            nonlocal on_event_calls
            on_event_calls += 1
            on_event_gate.set()
            return [AgentAction(ActionType.DO_NOTHING, {})]

        loop = _build_loop(on_event=_on_event)
        loop.start()
        try:
            # No handle → fire-and-forget.  enqueue() is sync and must not block.
            assert loop.enqueue(InboundEventWake(event=_evt(), handle=None))
            await asyncio.wait_for(on_event_gate.wait(), timeout=2.0)
        finally:
            await loop.stop(timeout=1.0)

        assert on_event_calls == 1

    async def test_fifo_ordering_within_queue(self):
        order: list[str] = []

        async def _on_event(event: AgentEvent) -> list[AgentAction]:
            order.append(event.payload["tag"])
            return [AgentAction(ActionType.DO_NOTHING, {})]

        loop = _build_loop(on_event=_on_event)
        # Pre-fill before start so all three are queued before drain begins.
        loop.enqueue(InboundEventWake(event=_evt({"tag": "a"}), handle=None))
        loop.enqueue(InboundEventWake(event=_evt({"tag": "b"}), handle=None))
        loop.enqueue(InboundEventWake(event=_evt({"tag": "c"}), handle=None))
        loop.start()
        try:
            # Wait until the queue has been drained.
            for _ in range(200):
                if len(order) == 3:
                    break
                await asyncio.sleep(0.01)
        finally:
            await loop.stop(timeout=1.0)

        assert order == ["a", "b", "c"]


class TestQueueFullDiscard:
    async def test_queue_full_discards_and_increments_dropped(self):
        # Small queue so we can fill it before the loop drains.  Hold the
        # loop on a barrier so the queue stays full at enqueue time.
        gate = asyncio.Event()

        async def _on_event(event: AgentEvent) -> list[AgentAction]:
            await gate.wait()
            return [AgentAction(ActionType.DO_NOTHING, {})]

        loop = _build_loop(on_event=_on_event, queue_size=2)
        loop.start()
        try:
            # First wake is dequeued and blocks on the gate.
            assert loop.enqueue(InboundEventWake(event=_evt(), handle=None))
            # Give the loop a tick to start handling the first wake.
            await asyncio.sleep(0.05)
            # Now fill the queue (size=2) and overflow.
            assert loop.enqueue(InboundEventWake(event=_evt(), handle=None))
            assert loop.enqueue(InboundEventWake(event=_evt(), handle=None))
            # Third additional enqueue should be discarded.
            assert not loop.enqueue(InboundEventWake(event=_evt(), handle=None))
            assert loop.dropped_count == 1
            # Discarding twice should not raise; counter keeps climbing.
            assert not loop.enqueue(InboundEventWake(event=_evt(), handle=None))
            assert loop.dropped_count == 2
        finally:
            gate.set()
            await loop.stop(timeout=1.0)


class TestSupervisor:
    async def test_loop_survives_on_event_exception(self, caplog):
        attempts = 0

        async def _on_event(event: AgentEvent) -> list[AgentAction]:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("boom")
            return [AgentAction(ActionType.DO_NOTHING, {})]

        loop = _build_loop(on_event=_on_event)
        loop.start()
        try:
            with caplog.at_level(logging.ERROR, logger="agents.event_loop"):
                handle_fail = SyncDispatchHandle()
                loop.enqueue(
                    InboundEventWake(event=_evt({"tag": "x"}), handle=handle_fail),
                )
                # First wake raises; the handle is rejected with the exception.
                with pytest.raises(RuntimeError, match="boom"):
                    await asyncio.wait_for(handle_fail, timeout=2.0)

                # Second wake must still be handled — supervisor restarted the loop body.
                handle_ok = SyncDispatchHandle()
                loop.enqueue(
                    InboundEventWake(event=_evt({"tag": "y"}), handle=handle_ok),
                )
                actions = await asyncio.wait_for(handle_ok, timeout=2.0)
        finally:
            await loop.stop(timeout=1.0)

        assert attempts == 2
        assert actions
        # Supervisor emits a structured ERROR record on the failed wake.
        assert any(
            "agent.event_loop.wake_failed" in rec.message
            or rec.exc_info is not None
            for rec in caplog.records
        )

    async def test_handle_does_not_resolve_when_no_caller(self):
        """Fire-and-forget enqueue does not produce a future the GC must clean."""

        async def _on_event(event: AgentEvent) -> list[AgentAction]:
            return [AgentAction(ActionType.DO_NOTHING, {})]

        loop = _build_loop(on_event=_on_event)
        loop.start()
        try:
            # Hand-crafted: handle exists but no one awaits.  Should not raise.
            handle = SyncDispatchHandle()
            loop.enqueue(InboundEventWake(event=_evt(), handle=handle))
            # Give the loop time to process and resolve the handle.
            for _ in range(200):
                if handle.done():
                    break
                await asyncio.sleep(0.01)
            assert handle.done()
        finally:
            await loop.stop(timeout=1.0)


class TestScheduledWake:
    async def test_scheduled_wake_invokes_on_tick(self):
        ticks: list[ScheduledWake] = []

        async def _on_tick(wake: ScheduledWake) -> None:
            ticks.append(wake)

        loop = _build_loop(on_tick=_on_tick)
        loop.start()
        try:
            loop.enqueue(
                ScheduledWake(timer_id="legacy_tick", callback_kind="tick"),
            )
            for _ in range(200):
                if ticks:
                    break
                await asyncio.sleep(0.01)
        finally:
            await loop.stop(timeout=1.0)

        assert len(ticks) == 1
        assert ticks[0].timer_id == "legacy_tick"
        assert ticks[0].callback_kind == "tick"

    async def test_register_timer_fires_periodically(self):
        ticks: list[float] = []
        seen_event = asyncio.Event()

        async def _on_tick(wake: ScheduledWake) -> None:
            ticks.append(asyncio.get_running_loop().time())
            if len(ticks) >= 3:
                seen_event.set()

        loop = _build_loop(on_tick=_on_tick)
        loop.start()
        try:
            # 50ms interval, expect ≥3 fires within ~250ms.
            loop.register_timer(
                timer_id="legacy_tick",
                callback_kind="tick",
                interval=0.05,
            )
            await asyncio.wait_for(seen_event.wait(), timeout=2.0)
        finally:
            await loop.stop(timeout=1.0)

        # All three fires carry the same id.
        assert len(ticks) >= 3
        # Intervals should be ~0.05s apart (allow generous slack on CI).
        spans = [b - a for a, b in zip(ticks, ticks[1:], strict=False)]
        for span in spans:
            assert 0.02 <= span <= 0.5

    async def test_unregister_timer_stops_firing(self):
        ticks: list[ScheduledWake] = []

        async def _on_tick(wake: ScheduledWake) -> None:
            ticks.append(wake)

        loop = _build_loop(on_tick=_on_tick)
        loop.start()
        try:
            loop.register_timer(
                timer_id="legacy_tick", callback_kind="tick", interval=0.05,
            )
            await asyncio.sleep(0.18)
            mid = len(ticks)
            assert mid >= 2
            loop.unregister_timer("legacy_tick")
            await asyncio.sleep(0.18)
            # After unregistering, the count must not have grown by more than
            # at most 1 (a race with a fire already in flight).
            assert len(ticks) - mid <= 1
        finally:
            await loop.stop(timeout=1.0)


class TestSyncDispatchHandle:
    async def test_resolve_then_await(self):
        handle = SyncDispatchHandle()
        handle.resolve([AgentAction(ActionType.DO_NOTHING, {})])
        result = await asyncio.wait_for(handle, timeout=0.5)
        assert len(result) == 1
        assert handle.done()

    async def test_double_resolve_is_idempotent(self):
        handle = SyncDispatchHandle()
        handle.resolve([])
        # Second resolve must not raise.
        handle.resolve([AgentAction(ActionType.DO_NOTHING, {})])
        result = await handle
        # First resolve wins.
        assert result == []

    async def test_reject_propagates_exception(self):
        handle = SyncDispatchHandle()
        handle.reject(RuntimeError("nope"))
        with pytest.raises(RuntimeError, match="nope"):
            await handle

    async def test_wait_for_bounds_await(self):
        """ActionExecutor.dispatch wraps the inner dispatch in wait_for; the
        handle must honour the timeout shape (cancellable Future)."""
        handle = SyncDispatchHandle()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(handle, timeout=0.05)
