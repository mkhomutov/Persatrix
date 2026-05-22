"""Wake-taxonomy types and SyncDispatchHandle — split out of ``agents.event_loop``.

Isolated here so the lifecycle and supervisor modules can import the types
without a circular dependency on the main :mod:`agents.event_loop` module.
The :class:`EventLoop` class re-exports all public names via its ``__all__``
so existing ``from agents.event_loop import InboundEventWake`` call sites
remain unchanged.
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .memory._events import MemoryWriteEvent
    from .persona_types import AgentAction, AgentEvent

__all__ = [
    "InboundEventWake",
    "SalienceWake",
    "ScheduledWake",
    "SyncDispatchHandle",
    "WakeEvent",
]


# ─── Wake taxonomy ──────────────────────────────────────────────────────────


class WakeEvent:
    """Marker base for the three wake variants drained by :class:`EventLoop`.

    Variants are dataclasses below; :class:`SalienceWake` is *declared* on
    the taxonomy so the loop's ``isinstance`` dispatch is exhaustive from
    Phase 1, but no producer enqueues it. PR 3b (RFC 0024 Phase 3) wires
    the producer.
    """


@dataclass
class InboundEventWake(WakeEvent):
    """Inbound RPC / channel-message wake carrying an :class:`AgentEvent`.

    ``handle`` is ``None`` for fire-and-forget wakes (the producer does not
    need the agent's action list).  When set, the loop resolves the handle
    with the agent's ``list[AgentAction]`` after ``on_event`` completes —
    this is the load-bearing path for chat-style callers that extract the
    reply text from the returned actions.
    """

    event: AgentEvent
    handle: SyncDispatchHandle | None = None


@dataclass
class ScheduledWake(WakeEvent):
    """Scheduled-timer fire — ``ScheduledWake(timer_id="legacy_tick", callback_kind="tick")``
    is the v0.3.2 ``tick_interval_seconds`` cadence under the adapter."""

    timer_id: str
    callback_kind: str


@dataclass
class SalienceWake(WakeEvent):
    """Memory-write-triggered wake (RFC 0024 Phase 3 / PR 3b).

    Declared so the ``isinstance`` dispatch in :meth:`EventLoop._handle_wake`
    is exhaustive from Phase 1.  PR 3b wires the producer
    (``MemoryWriteBus`` subscriber) and the consumer.
    """

    # ``None`` only for the Phase-1 placeholder construction path; PR 3b's
    # subscriber always builds this with a concrete ``MemoryWriteEvent``.
    write_event: MemoryWriteEvent | None = field(default=None)


# ─── SyncDispatchHandle ─────────────────────────────────────────────────────


class SyncDispatchHandle:
    """``asyncio.Future``-shaped helper the loop resolves after ``on_event``.

    Idempotent: a second :meth:`resolve` / :meth:`reject` call is silently
    ignored so the supervisor can safely reject on exception even if the
    handler already resolved. ``__await__`` returns the underlying
    future's iterator, so :func:`asyncio.wait_for` cancellation propagates
    correctly.
    """

    __slots__ = ("_future",)

    def __init__(self) -> None:
        # ``get_running_loop()`` (not ``get_event_loop()``): the latter is
        # deprecated since 3.10 when no running loop exists and is set to
        # be removed in 3.12+.  This class is only ever instantiated from
        # inside an ``async def`` (``EventDispatcher.dispatch`` and the
        # EventLoop supervisor body), so a running loop is guaranteed.
        self._future: asyncio.Future[list[AgentAction]] = (
            asyncio.get_running_loop().create_future()
        )

    def resolve(self, value: list[AgentAction]) -> None:
        if not self._future.done():
            self._future.set_result(value)

    def reject(self, exc: BaseException) -> None:
        if not self._future.done():
            self._future.set_exception(exc)

    def done(self) -> bool:
        return self._future.done()

    def __await__(self) -> Generator[Any, None, list[AgentAction]]:
        # Annotated return shape pins the awaited result type to
        # ``list[AgentAction]`` so callers (``EventDispatcher.dispatch``)
        # do not widen ``actions`` to ``Any``.  Only the third type
        # parameter is load-bearing for call-site typing; the yielded
        # ``Any`` matches the standard ``Future.__await__`` shape and is
        # not part of the contract.  (PR 1 review finding #5.)
        return self._future.__await__()
