"""Tests for RFC 0024 PR 5 lifecycle-safety fixes.

Pins the contracts added in PR 5:

* **Reentrant-dispatch escape hatch** (:class:`TestReentrantDispatchDeadlock`)
  — ``on_event`` re-dispatching to the same agent via a handle-bearing
  :class:`InboundEventWake` resolves instead of FIFO-starving.

* **Stop-time handle drain** (:class:`TestStopSettlesPendingHandles`) —
  wakes queued behind the in-flight one when ``stop()`` fires have their
  handles resolved with the empty discard result; an in-flight reentrant
  resolver's handle is settled with ``[]`` *before* ``stop()`` returns.

* **Queue-full WARNING throttle** (:class:`TestQueueFullLogThrottle`) —
  the per-drop WARNING is emitted at most once per
  ``_QUEUE_FULL_LOG_INTERVAL_S``.

Reentrant-resolver observability-parity tests (the ``agent.wake.inbound``
counter + ``wake_failed`` log) live in the sibling
``test_event_loop_observability.py`` — split off so this file (pure
correctness contracts) stays under the 500-line limit once the
in-flight-handle hard-cancel test landed.

Split out of ``test_event_loop.py`` for file-size review-friendliness
(RFC 0024 PR 5 — both files exceeded the 500-line limit).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

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


class TestQueueFullLogThrottle:
    """RFC 0024 PR 1 review finding #2 — the queue-full WARNING is rate-limited.

    PR 4 made channel-message drops a steady-state backpressure signal, so
    the original per-drop WARNING flooded operator logs.  The log line is
    now throttled to once per ``_QUEUE_FULL_LOG_INTERVAL_S`` while the
    per-drop signal (``dropped_count`` + the ``agent.wake.dropped`` counter)
    still moves on every drop.  The supervisor is left unstarted so the
    queue stays full and every overflow takes the discard path.
    """

    async def test_warning_throttled_within_interval(self, caplog):
        clock = {"t": 100.0}
        loop = _build_loop(queue_size=1)
        loop._monotonic = lambda: clock["t"]
        # Fill the single slot; subsequent enqueues overflow.
        assert loop.enqueue(ScheduledWake(timer_id="t", callback_kind="tick"))
        with caplog.at_level(logging.WARNING, logger="agents.event_loop"):
            for _ in range(5):
                assert not loop.enqueue(
                    ScheduledWake(timer_id="t", callback_kind="tick"),
                )
            warnings = [
                r for r in caplog.records if "queue_full" in r.getMessage()
            ]
        # Five drops, one WARNING — the rest were within the throttle window.
        assert len(warnings) == 1
        assert loop.dropped_count == 5

    async def test_warning_re_emits_after_interval(self, caplog):
        clock = {"t": 100.0}
        loop = _build_loop(queue_size=1)
        loop._monotonic = lambda: clock["t"]
        assert loop.enqueue(ScheduledWake(timer_id="t", callback_kind="tick"))
        with caplog.at_level(logging.WARNING, logger="agents.event_loop"):
            assert not loop.enqueue(
                ScheduledWake(timer_id="t", callback_kind="tick"),
            )  # first drop logs
            # Advance past the throttle window — the next drop logs again.
            clock["t"] += loop._QUEUE_FULL_LOG_INTERVAL_S + 1.0
            assert not loop.enqueue(
                ScheduledWake(timer_id="t", callback_kind="tick"),
            )
            warnings = [
                r for r in caplog.records if "queue_full" in r.getMessage()
            ]
        assert len(warnings) == 2
        assert loop.dropped_count == 2


class TestReentrantDispatchDeadlock:
    """RFC 0024 PR 1 review finding #1 — reentrant dispatch (now fixed in PR 5).

    ``on_event(A)`` re-dispatching to A enqueues a handle-bearing
    :class:`InboundEventWake` on A's own queue and awaits the handle.  The
    single supervisor is blocked inside the outer ``on_event`` body, so a
    queued inner wake would never drain — FIFO starvation that previously
    left the handle unresolved until the caller's dispatch-timeout.

    PR 5 added the reentrancy escape hatch in :meth:`EventLoop.enqueue`: a
    handle-bearing inbound wake enqueued from inside the loop's own
    supervisor task runs ``on_event`` on a detached resolver task instead
    of the FIFO, so the awaiter resolves with the inner result.  This test
    pins that contract (reentrant dispatch completes).
    """

    async def test_reentrant_dispatch_completes(self):
        loop_ref: list[EventLoop] = []

        async def _on_event(event: AgentEvent) -> list[AgentAction]:
            if event.payload.get("level") == "outer":
                # Reentrant: enqueue an inner wake on the same loop and
                # await its handle.  Under the pre-fix shape this deadlocks
                # because the supervisor cannot drain the inner wake until
                # this outer ``on_event`` returns.
                inner_handle = SyncDispatchHandle()
                loop_ref[0].enqueue(
                    InboundEventWake(
                        event=AgentEvent(
                            event_type=EventType.CHANNEL_MESSAGE,
                            payload={"level": "inner"},
                        ),
                        handle=inner_handle,
                    ),
                )
                inner_actions = await inner_handle
                return inner_actions
            return [AgentAction(ActionType.COMPLETE_TASK, {"result": "inner_ok"})]

        loop = _build_loop(on_event=_on_event)
        loop_ref.append(loop)
        loop.start()
        try:
            outer_handle = SyncDispatchHandle()
            loop.enqueue(
                InboundEventWake(
                    event=_evt({"level": "outer"}),
                    handle=outer_handle,
                ),
            )
            # Tight timeout: a regression (reentrancy escape hatch removed)
            # surfaces as TimeoutError rather than hanging the suite.
            actions = await asyncio.wait_for(outer_handle, timeout=0.5)
            # The reentrant inner wake runs on a detached resolver task and
            # the outer handle resolves with the inner result.
            assert actions and actions[0].action_type == ActionType.COMPLETE_TASK
        finally:
            await loop.stop(timeout=1.0)


class TestStopSettlesPendingHandles:
    """RFC 0024 PR 1 review finding #5 — stop() settles pending handles (fixed in PR 5).

    When :meth:`EventLoop.stop` is called while wakes remain queued, the
    supervisor's ``while not self._stopped.is_set()`` guard breaks out
    before draining them.  Previously the orphaned handles never resolved,
    so chat-style callers hung until their external ``wait_for`` deadline
    (chat path: clamped timeout; in-process cascade: 60 s default).

    PR 5 added :meth:`EventLoop._drain_pending_handles`: after the
    supervisor task exits, ``stop()`` drains residual queue items and
    resolves each pending :class:`InboundEventWake` handle with the empty
    action list — the same discard-contract result a queue-full drop
    yields.  The companion ``_stopped`` guard in :meth:`EventLoop.enqueue`
    closes the producer-side TOCTOU window.
    """

    async def test_stop_settles_pending_handles(self):
        block_on_event = asyncio.Event()
        in_flight = asyncio.Event()

        async def _on_event(event: AgentEvent) -> list[AgentAction]:
            in_flight.set()
            await block_on_event.wait()
            return [AgentAction(ActionType.DO_NOTHING, {})]

        loop = _build_loop(on_event=_on_event)
        loop.start()
        try:
            first = SyncDispatchHandle()
            second = SyncDispatchHandle()
            third = SyncDispatchHandle()

            assert loop.enqueue(InboundEventWake(event=_evt(), handle=first))
            # Wait for the first wake to reach the gate inside ``on_event``
            # so the next two enqueues land behind it in the FIFO queue.
            await asyncio.wait_for(in_flight.wait(), timeout=2.0)
            assert loop.enqueue(InboundEventWake(event=_evt(), handle=second))
            assert loop.enqueue(InboundEventWake(event=_evt(), handle=third))

            # Schedule ``stop()`` and yield once so its synchronous prefix
            # (``_stopped.set()`` + sentinel enqueue) runs *before* the gate
            # releases the in-flight wake — second/third are then still
            # queued (the supervisor breaks on ``_stopped`` without draining
            # them) when stop()'s drain settles them.
            stop_task = asyncio.create_task(loop.stop(timeout=2.0))
            await asyncio.sleep(0)
            block_on_event.set()
            await stop_task

            # First wake resolved during in-flight completion — its
            # ``handle.resolve`` ran inside ``_handle_wake`` before the
            # supervisor's loop-iter ``_stopped`` check exited the body.
            assert first.done()
            # second/third were orphaned in the queue; stop() drains and
            # resolves them with the empty discard result so the awaiting
            # caller settles instead of hanging.
            assert await asyncio.wait_for(second, timeout=0.5) == []
            assert await asyncio.wait_for(third, timeout=0.5) == []
        finally:
            # Defensive: release the gate in case the test failed before
            # ``block_on_event.set()`` so the supervisor does not hang the
            # test runner.
            if not block_on_event.is_set():
                block_on_event.set()

    async def test_enqueue_after_stop_rejects_and_settles_handle(self):
        """TOCTOU guard — a handle-bearing enqueue racing after ``stop()``
        is rejected and its handle settled with the discard result, never
        orphaned on a queue no supervisor will drain."""
        loop = _build_loop()
        loop.start()
        await loop.stop(timeout=1.0)

        handle = SyncDispatchHandle()
        accepted = loop.enqueue(InboundEventWake(event=_evt(), handle=handle))
        assert accepted is False
        assert await asyncio.wait_for(handle, timeout=0.5) == []

    async def test_stop_settles_in_flight_reentrant_handle(self):
        """A reentrant resolver still running ``on_event`` at ``stop()`` has
        its handle settled with the empty discard result *before* ``stop()``
        returns — not orphaned, and not settled only on a later tick.

        ``stop()`` cancels the detached resolver task and **awaits** it, so
        the resolver's cancellation handler (which resolves the handle with
        ``[]``) has run by the time ``stop()`` returns.  Cancelling the
        resolver also settles the inner handle the blocked supervisor is
        awaiting, letting the supervisor drain gracefully instead of being
        hard-cancelled at the stop timeout. (PR 5 re-review — review #2/#4.)
        """
        inner_started = asyncio.Event()
        block = asyncio.Event()
        inner_handle_ref: list[SyncDispatchHandle] = []
        outer_handle_ref: list[SyncDispatchHandle] = []
        loop_ref: list[EventLoop] = []

        async def _on_event(event: AgentEvent) -> list[AgentAction]:
            if event.payload.get("level") == "outer":
                # Reentrant: enqueue an inner handle-bearing wake and await
                # it.  The escape hatch runs the inner ``on_event`` on a
                # detached resolver task while this outer body (and thus the
                # supervisor) blocks here.
                inner_handle = SyncDispatchHandle()
                inner_handle_ref.append(inner_handle)
                loop_ref[0].enqueue(
                    InboundEventWake(
                        event=_evt({"level": "inner"}), handle=inner_handle,
                    ),
                )
                return await inner_handle
            # Inner: park so the resolver task is still in flight at stop().
            inner_started.set()
            await block.wait()
            return [AgentAction(ActionType.DO_NOTHING, {})]

        loop = _build_loop(on_event=_on_event)
        loop_ref.append(loop)
        loop.start()
        try:
            outer_handle = SyncDispatchHandle()
            outer_handle_ref.append(outer_handle)
            loop.enqueue(
                InboundEventWake(
                    event=_evt({"level": "outer"}), handle=outer_handle,
                ),
            )
            # Wait until the reentrant resolver is parked inside the inner
            # ``on_event`` so it is genuinely in flight when stop() fires.
            await asyncio.wait_for(inner_started.wait(), timeout=2.0)

            await loop.stop(timeout=2.0)

            # The contract: the resolver's handle is settled by the time
            # stop() returns (it is already done — no further await needed).
            assert inner_handle_ref[0].done()
            assert await asyncio.wait_for(inner_handle_ref[0], timeout=0.5) == []
            # The outer body resumes with the inner ``[]`` and the supervisor
            # resolves the outer handle, so it settles too rather than orphan.
            assert await asyncio.wait_for(outer_handle_ref[0], timeout=0.5) == []
        finally:
            if not block.is_set():
                block.set()

    async def test_stop_settles_in_flight_handle_on_hard_cancel(self):
        """A handle-bearing FIFO wake whose ``on_event`` is still running when
        ``stop()`` hard-cancels the supervisor (stop timeout exceeded) has its
        handle settled with the empty discard result, not orphaned.

        Unlike the queued wakes :meth:`_drain_pending_handles` settles, the
        *in-flight* wake has already been dequeued, so the drain cannot reach
        it.  When ``on_event`` outlives the stop timeout the supervisor is
        cancelled mid-wake; the cancellation must settle the handle with
        ``[]`` — parity with the reentrant resolver's cancellation path
        (:meth:`_spawn_reentrant`) and the queued-wake drain — so the awaiting
        caller resolves immediately instead of hanging to its own external
        ``wait_for`` deadline. (PR 5 re-review.)
        """
        block = asyncio.Event()
        in_flight = asyncio.Event()

        async def _on_event(event: AgentEvent) -> list[AgentAction]:
            in_flight.set()
            # Never released: forces stop() to hit its timeout and
            # hard-cancel the supervisor while this wake is in flight.
            await block.wait()
            return [AgentAction(ActionType.DO_NOTHING, {})]

        loop = _build_loop(on_event=_on_event)
        loop.start()
        try:
            handle = SyncDispatchHandle()
            assert loop.enqueue(InboundEventWake(event=_evt(), handle=handle))
            await asyncio.wait_for(in_flight.wait(), timeout=2.0)

            # Short timeout: on_event blocks past it, so stop() hard-cancels
            # the supervisor.  The in-flight handle must be settled by the
            # time stop() returns.
            await loop.stop(timeout=0.1)

            assert handle.done()
            assert await asyncio.wait_for(handle, timeout=0.5) == []
        finally:
            if not block.is_set():
                block.set()
