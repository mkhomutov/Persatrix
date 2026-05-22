"""RFC 0024 Phase 4 — fire-and-forget inbound (``on_inbound``) routing.

Split from ``test_event_loop.py`` to keep both files under the project's
500-line code-file cap.

A handle-less :class:`~agents.event_loop.InboundEventWake` (a channel
message) routes to the loop's ``on_inbound`` callback, which owns the full
decide → execute → recover lifecycle. A handle-bearing wake (chat /
in-process cascade) keeps the synchronous-reply path: ``on_event`` runs
and resolves the :class:`~agents.event_loop.SyncDispatchHandle`, and
``on_inbound`` must NOT run.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from agents.event_loop import (
    EventLoop,
    InboundEventWake,
    ScheduledWake,
    SyncDispatchHandle,
)
from agents.persona_types import ActionType, AgentAction, AgentEvent, EventType


def _evt(payload: dict[str, Any] | None = None) -> AgentEvent:
    return AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload=payload or {"content": "hi"},
    )


def _build_loop(
    *,
    on_event: Callable[[AgentEvent], Awaitable[list[AgentAction]]] | None = None,
    on_inbound: Callable[[AgentEvent], Awaitable[None]] | None = None,
) -> EventLoop:
    async def _default_event(_event: AgentEvent) -> list[AgentAction]:
        return [AgentAction(ActionType.DO_NOTHING, {})]

    async def _default_tick(_wake: ScheduledWake) -> None:
        return None

    return EventLoop(
        agent_id="inbound-agent",
        on_event=on_event or _default_event,
        on_tick=_default_tick,
        on_inbound=on_inbound,
    )


class TestFireAndForgetInbound:
    async def test_handleless_inbound_routes_to_on_inbound(self):
        inbound_seen: list[AgentEvent] = []
        on_event_calls = 0
        done = asyncio.Event()

        async def _on_event(_event: AgentEvent) -> list[AgentAction]:
            nonlocal on_event_calls
            on_event_calls += 1
            return [AgentAction(ActionType.DO_NOTHING, {})]

        async def _on_inbound(event: AgentEvent) -> None:
            inbound_seen.append(event)
            done.set()

        loop = _build_loop(on_event=_on_event, on_inbound=_on_inbound)
        loop.start()
        try:
            assert loop.enqueue(
                InboundEventWake(event=_evt({"content": "channel"}), handle=None),
            )
            await asyncio.wait_for(done.wait(), timeout=2.0)
        finally:
            await loop.stop(timeout=1.0)

        # Fire-and-forget path: on_inbound ran with the event; on_event
        # (the synchronous-reply callback) was never touched.
        assert len(inbound_seen) == 1
        assert inbound_seen[0].payload["content"] == "channel"
        assert on_event_calls == 0

    async def test_handle_bearing_inbound_still_uses_on_event(self):
        """A handle-bearing wake (chat / cascade) keeps the synchronous-reply
        path even when ``on_inbound`` is wired — on_inbound must NOT run."""
        inbound_calls = 0

        async def _on_event(_event: AgentEvent) -> list[AgentAction]:
            return [AgentAction(ActionType.COMPLETE_TASK, {"result": "sync"})]

        async def _on_inbound(_event: AgentEvent) -> None:
            nonlocal inbound_calls
            inbound_calls += 1

        loop = _build_loop(on_event=_on_event, on_inbound=_on_inbound)
        loop.start()
        try:
            handle = SyncDispatchHandle()
            assert loop.enqueue(InboundEventWake(event=_evt(), handle=handle))
            actions = await asyncio.wait_for(handle, timeout=2.0)
        finally:
            await loop.stop(timeout=1.0)

        assert actions[0].payload["result"] == "sync"
        assert inbound_calls == 0

    async def test_handleless_inbound_without_on_inbound_falls_back_to_on_event(self):
        """With no ``on_inbound`` wired, a handle-less wake falls back to
        ``on_event`` (actions discarded) — the generic-loop default."""
        on_event_calls = 0
        done = asyncio.Event()

        async def _on_event(_event: AgentEvent) -> list[AgentAction]:
            nonlocal on_event_calls
            on_event_calls += 1
            done.set()
            return [AgentAction(ActionType.DO_NOTHING, {})]

        loop = _build_loop(on_event=_on_event)  # on_inbound=None
        loop.start()
        try:
            assert loop.enqueue(InboundEventWake(event=_evt(), handle=None))
            await asyncio.wait_for(done.wait(), timeout=2.0)
        finally:
            await loop.stop(timeout=1.0)

        assert on_event_calls == 1
