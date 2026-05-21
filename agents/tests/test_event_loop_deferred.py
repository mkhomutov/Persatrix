"""RFC 0024 PR 1 deferred-fix regression pins.

Each test in this module is marked ``xfail(strict=True)`` and pins a
contract whose fix is tracked as a deferred item in
``docs/rfcs/0024-pr-plan.md`` "From PR 1 review" — the strict marker
forces an XPASS to fail the suite the moment the fix lands, prompting
the maintainer to remove the marker.

Kept in a separate module from ``test_event_loop.py`` so the substrate's
green-path coverage stays focused and review-friendly under the 500-line
code-file cap; the green-path file ships the contracts a passing PR
upholds, this file ships the known-broken edges a future PR will fix.
"""

from __future__ import annotations

import asyncio
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


# Helpers duplicated from ``test_event_loop.py`` so this module stays
# independent — pytest does not cross-import between sibling test files,
# and the helpers are small enough that duplication is cheaper than a
# shared ``conftest.py`` fixture (which would also have to be threaded
# through the green-path module).
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


class TestStopDrainsPendingHandles:
    """RFC 0024 PR 1 follow-up review — stop drops pending handles silently.

    When :meth:`EventLoop.stop` is called while wakes remain queued, the
    supervisor's ``while not self._stopped.is_set()`` guard at the top of
    :meth:`_run` causes it to break out *before* the next ``queue.get()``.
    Concretely:

    1. Queue has [A (in flight), B, C] where B and C carry handles.
    2. ``stop()`` sets ``_stopped`` and enqueues ``_StopSentinel``.
    3. A's ``on_event`` completes — ``handle.resolve`` runs for A.
    4. Supervisor loops; the ``_stopped`` guard short-circuits ``break``.
    5. B and C are orphaned — their handles never resolve or reject.

    Any chat-style caller awaiting B or C then hangs until its external
    ``asyncio.wait_for`` deadline fires (chat path: clamped timeout;
    in-process cascade: 60 s default).  The same TOCTOU shape applies
    to :meth:`EventDispatcher.dispatch`: a producer that checks
    ``scheduler.is_running`` and then ``enqueue``-s while ``stop()``
    races in between has its handle hang for the same root cause.

    The follow-up reviewer flagged this as deferred (tracked in
    ``docs/rfcs/0024-pr-plan.md`` "From PR 1 review" item 5) and
    requested a regression test now so the orphan behaviour has a
    permanent home.  The test pins the *intended* contract (``stop()``
    settles every pending handle before it returns — resolve on drain
    or reject with a shutdown exception) and is marked
    ``xfail(strict=True)`` so that once the fix lands the XPASS forces
    marker removal.
    """

    @pytest.mark.xfail(
        reason=(
            "RFC 0024 PR 1 follow-up review — stop() drops pending "
            "InboundEventWake handles silently; deferred for a follow-up "
            "PR per docs/rfcs/0024-pr-plan.md item (5)."
        ),
        strict=True,
        raises=TimeoutError,
    )
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
            # (``_stopped.set()`` + sentinel enqueue) runs *before* the
            # gate releases the in-flight wake.  Without the yield the
            # supervisor could drain second/third after first completes
            # but before ``_stopped`` is set, masking the orphan bug and
            # causing a spurious XPASS.
            stop_task = asyncio.create_task(loop.stop(timeout=2.0))
            await asyncio.sleep(0)
            block_on_event.set()
            await stop_task

            # First wake resolved during in-flight completion — its
            # ``handle.resolve`` ran inside ``_handle_wake`` before the
            # supervisor's loop-iter ``_stopped`` check exited the body.
            assert first.done()
            # Intended contract: pending handles must be settled before
            # ``stop()`` returns.  Under the pre-fix shape they stay
            # pending forever; ``wait_for`` converts the hang into
            # ``TimeoutError`` so the ``xfail`` marker picks it up.
            await asyncio.wait_for(second, timeout=0.5)
            await asyncio.wait_for(third, timeout=0.5)
        finally:
            # Defensive: release the gate in case the test failed before
            # ``block_on_event.set()`` so the supervisor does not hang
            # the test runner.
            if not block_on_event.is_set():
                block_on_event.set()
