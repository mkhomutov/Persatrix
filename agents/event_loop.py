"""Persatrix per-agent event loop (RFC 0024 Phase 1).

Single :class:`asyncio.Queue` per agent draining wake events FIFO. Replaces
the v0.3.2 polling timer at the substrate level; the public surfaces
(:class:`agents.tick.TickScheduler`, :class:`agents.dispatch.EventDispatcher`)
keep their contracts and become thin adapters over this loop.

The :class:`SyncDispatchHandle` preserves the three contracts that ride on
``EventDispatcher.dispatch()``:

* **Return value** — ``SendChatMessage`` extracts the reply text from the
  ``list[AgentAction]`` the dispatcher returns. The handle resolves with
  that list after :meth:`on_event` completes.
* **Await/serialisation** — :class:`agents.action_executor.ActionExecutor`
  wraps the dispatch in :func:`asyncio.wait_for` for timeout bounding.
  The handle is an :class:`asyncio.Future` so ``wait_for`` cancels it
  cleanly on timeout.
* **Queue ordering** — every wake (inbound, scheduled, salience) drains
  through the same FIFO queue; ordering between an inbound RPC event and
  a scheduled-timer firing is not promised by the RFC (see RFC 0024 §B)
  but ordering *within* either variant is.

Sub-agents do **not** inherit an :class:`EventLoop` per
:doc:`RFC 0024 Decided §2 <../docs/rfcs/0024-event-driven-scheduling>` —
``SubAgentSpawner.dispatch`` continues to call ``BaseAgent.handle()``
synchronously and never enqueues an :class:`InboundEventWake`.

Backpressure is **discard, not block** per RFC 0024 Decided §1: a full
queue rejects new wakes via :meth:`EventLoop.enqueue` returning ``False``
and increments :attr:`EventLoop.dropped_count`. Phase 4 wires the
``agent.wake.dropped`` OTEL counter once channel-message dispatch is the
dominant producer.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable, Generator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .persona_types import AgentAction, AgentEvent

logger = logging.getLogger(__name__)

__all__ = [
    "EventLoop",
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

    # TODO(Phase 3b — RFC 0024 PR 3b): tighten the type once the
    # ``MemoryWriteEvent`` (write-side salience producer) lands.  Kept as
    # ``Any`` here so Phase 1 does not import a type that does not yet
    # exist; PR 3b will introduce the concrete class and replace this.
    write_event: Any = field(default=None)


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


# ─── EventLoop ──────────────────────────────────────────────────────────────


@dataclass
class _TimerEntry:
    """Periodic or one-shot timer registry entry.

    ``interval=None`` and ``one_shot=True`` marks a one-shot — fires once
    at the initial delay then self-removes.  ``jitter_max=0.0`` skips
    ``random.uniform`` entirely so the legacy adapter cadence stays
    deterministic (pinned by ``test_jitter_zero_default``).
    """

    interval: float | None
    callback_kind: str
    jitter_max: float = 0.0
    handle: asyncio.TimerHandle | None = None
    cancelled: bool = False
    one_shot: bool = False


class EventLoop:
    """Per-agent wake queue + supervisor.

    Callback-driven so :class:`agents.tick.TickScheduler` can wire its idle
    accounting and the existing ``ActionExecutor`` plumbing without the
    loop importing the persona surface.

    ``on_event`` is called for every :class:`InboundEventWake`; its return
    value resolves the wake's handle when one is attached.  ``on_tick`` is
    called for every :class:`ScheduledWake` — return value is ignored
    (scheduled wakes have no caller awaiting actions).
    """

    _DEFAULT_QUEUE_SIZE = 1024

    # RFC 0024 §Security Considerations busy-loop guard floor; mirrors
    # ``TickScheduler._MIN_INTERVAL`` and the schema's ``minimum: 1.0``
    # on ``autonomy.timers[*].interval_seconds``.  Defense-in-depth for
    # any programmatic caller bypassing schema validation.
    _MIN_INTERVAL: float = 1.0

    def __init__(
        self,
        *,
        agent_id: str,
        on_event: Callable[[AgentEvent], Awaitable[list[AgentAction]]],
        on_tick: Callable[[ScheduledWake], Awaitable[None]],
        queue_size: int = _DEFAULT_QUEUE_SIZE,
    ) -> None:
        self._agent_id = agent_id
        self._on_event = on_event
        self._on_tick = on_tick
        self._queue: asyncio.Queue[WakeEvent] = asyncio.Queue(maxsize=queue_size)
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()
        self._timers: dict[str, _TimerEntry] = {}
        self._dropped_count = 0

    @property
    def agent_id(self) -> str:
        return self._agent_id

    @property
    def dropped_count(self) -> int:
        """Number of wakes discarded because the queue was full."""
        return self._dropped_count

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def task(self) -> asyncio.Task[None] | None:
        """The supervisor :class:`asyncio.Task` (or ``None`` before
        :meth:`start` and after :meth:`stop`).

        Exposed so adapters / tests do not reach into the private
        ``_task`` attribute — :class:`agents.tick.TickScheduler` keeps a
        ``_task`` back-compat shim that reads through this property for
        the pre-refactor ``test_start_idempotent`` assertion shape.
        """
        return self._task

    def has_timer(self, timer_id: str) -> bool:
        """Whether ``timer_id`` is currently registered.

        Public encapsulation boundary for :class:`agents.tick.TickScheduler`
        and adapters that need idempotent re-registration without reaching
        into :attr:`_timers`.
        """
        return timer_id in self._timers

    # ─── enqueue / drain ───────────────────────────────────────────────

    def enqueue(self, wake: WakeEvent) -> bool:
        """Enqueue a wake event. Returns ``True`` if accepted, ``False`` if dropped.

        Discard-not-block per RFC 0024 Decided §1. The dropped-count is
        observable via :attr:`dropped_count` for the ``agent.wake.dropped``
        counter Phase 4 wires.
        """
        try:
            self._queue.put_nowait(wake)
        except asyncio.QueueFull:
            self._dropped_count += 1
            logger.warning(
                "agent.event_loop.queue_full: agent_id=%s dropped_total=%d",
                self._agent_id,
                self._dropped_count,
            )
            return False
        return True

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopped.clear()
        self._task = asyncio.create_task(
            self._run(), name=f"event-loop-{self._agent_id}",
        )

    async def stop(self, timeout: float = 10.0) -> None:
        """Cancel timers and wait for the loop to drain in-flight wakes."""
        self._stopped.set()
        # Cancel all registered timers first so no new wakes are produced.
        for entry in list(self._timers.values()):
            entry.cancelled = True
            if entry.handle is not None:
                entry.handle.cancel()
        self._timers.clear()

        if self._task is not None and not self._task.done():
            # Poison the queue so the blocking ``get()`` returns and the
            # loop notices ``_stopped`` on its next iteration.
            try:
                self._queue.put_nowait(_StopSentinel())
            except asyncio.QueueFull:
                # Queue full — cancel the task directly.
                self._task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=timeout)
            except TimeoutError:
                logger.warning(
                    "agent.event_loop.stop_timeout: agent_id=%s timeout=%.1fs",
                    self._agent_id, timeout,
                )
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
            except asyncio.CancelledError:
                pass
        self._task = None

    # ─── timer registry ────────────────────────────────────────────────

    def register_timer(
        self,
        *,
        timer_id: str,
        callback_kind: str,
        interval: float | None = None,
        jitter_max: float = 0.0,
        fire_after: float | None = None,
    ) -> None:
        """Register a periodic or one-shot :class:`ScheduledWake` producer.

        Periodic timers pass ``interval`` (re-arms via ``call_later`` each
        fire, monotonic per RFC 0024 §C); one-shot timers pass
        ``fire_after`` (fires once at delay, then unregisters).
        ``jitter_max`` randomises each re-arm by ``±jitter_max`` seconds;
        ``0.0`` (default) skips the ``random.uniform`` call entirely.

        Raises ``ValueError`` if ``interval < _MIN_INTERVAL`` (busy-loop
        guard) or neither/both of ``interval`` and ``fire_after`` are set.
        """
        if timer_id in self._timers:
            raise ValueError(f"Timer {timer_id!r} already registered")
        if (interval is None) == (fire_after is None):
            raise ValueError(
                "register_timer requires exactly one of interval (periodic) "
                "or fire_after (one-shot)",
            )
        if interval is not None and interval < self._MIN_INTERVAL:
            raise ValueError(
                f"interval {interval}s below _MIN_INTERVAL "
                f"({self._MIN_INTERVAL}s) — busy-loop guard",
            )
        if fire_after is not None and fire_after <= 0.0:
            raise ValueError(f"fire_after {fire_after}s must be positive")

        entry = _TimerEntry(
            interval=interval,
            callback_kind=callback_kind,
            jitter_max=jitter_max,
            one_shot=fire_after is not None,
        )
        self._timers[timer_id] = entry
        self._arm_timer(timer_id, entry, initial_delay=fire_after)

    def unregister_timer(self, timer_id: str) -> None:
        entry = self._timers.pop(timer_id, None)
        if entry is None:
            return
        entry.cancelled = True
        if entry.handle is not None:
            entry.handle.cancel()

    def _arm_timer(
        self,
        timer_id: str,
        entry: _TimerEntry,
        *,
        initial_delay: float | None = None,
    ) -> None:
        # ``get_running_loop()`` (not ``get_event_loop()``): preempts the
        # 3.10+ deprecation.  Both ``register_timer`` (initial arm) and
        # ``_fire`` (re-arm) run inside the supervisor task, so a running
        # loop is guaranteed.
        def _fire() -> None:
            if entry.cancelled:
                return
            self.enqueue(
                ScheduledWake(timer_id=timer_id, callback_kind=entry.callback_kind),
            )
            if entry.one_shot:
                # One-shot timers self-clean so a subsequent register_timer
                # with the same id is not a conflict — pinned by
                # ``test_event_loop_timers.test_one_shot_fires_exactly_once``.
                self._timers.pop(timer_id, None)
                return
            if not entry.cancelled:
                entry.handle = asyncio.get_running_loop().call_later(
                    self._next_delay(entry), _fire,
                )

        first_delay = (
            initial_delay if initial_delay is not None else self._next_delay(entry)
        )
        entry.handle = asyncio.get_running_loop().call_later(first_delay, _fire)

    def _next_delay(self, entry: _TimerEntry) -> float:
        """Periodic re-arm delay; ``jitter_max=0.0`` returns exactly
        ``entry.interval`` and skips ``random.uniform`` for deterministic
        legacy-adapter cadence (pinned by ``test_jitter_zero_default``)."""
        assert entry.interval is not None  # noqa: S101
        if entry.jitter_max <= 0.0:
            return entry.interval
        return entry.interval + random.uniform(-entry.jitter_max, entry.jitter_max)

    # ─── supervisor ────────────────────────────────────────────────────

    async def _run(self) -> None:
        logger.info(
            "agent.event_loop.start: agent_id=%s queue_max=%d",
            self._agent_id, self._queue.maxsize,
        )
        try:
            while not self._stopped.is_set():
                try:
                    wake = await self._queue.get()
                except asyncio.CancelledError:
                    raise
                if isinstance(wake, _StopSentinel):
                    break
                await self._handle_wake_supervised(wake)
        finally:
            logger.info(
                "agent.event_loop.stop: agent_id=%s dropped_total=%d",
                self._agent_id, self._dropped_count,
            )

    async def _handle_wake_supervised(self, wake: WakeEvent) -> None:
        try:
            await self._handle_wake(wake)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — supervisor must not propagate
            wake_kind = _wake_kind(wake)
            logger.error(
                "agent.event_loop.wake_failed: agent_id=%s wake_kind=%s err=%r",
                self._agent_id, wake_kind, exc,
                exc_info=True,
            )
            # Reject any awaiting handle so the producer does not hang.
            if isinstance(wake, InboundEventWake) and wake.handle is not None:
                wake.handle.reject(exc)

    async def _handle_wake(self, wake: WakeEvent) -> None:
        if isinstance(wake, InboundEventWake):
            # Cancellation note (PR 1 review finding #2): when a chat-style
            # caller does ``asyncio.wait_for(handle, timeout=…)`` and the
            # deadline fires, ``wait_for`` cancels the handle's underlying
            # future but the supervisor task is unaware — it keeps running
            # ``self._on_event`` (and any LLM/tool-use round in flight)
            # to completion, after which ``handle.resolve`` no-ops because
            # the future is already done.  Net effect: timed-out chat
            # requests still pay for the full LLM call + tool round on the
            # wallet lease they were supposed to abort.  Pre-existing
            # shape (the non-reentrant agent lock had the same property in
            # v0.3.2), tracked as a deferred finding in
            # ``docs/rfcs/0024-pr-plan.md`` for a future "abort the
            # in-flight LLM call on caller cancellation" PR.
            actions = await self._on_event(wake.event)
            if wake.handle is not None:
                wake.handle.resolve(actions)
            return
        if isinstance(wake, ScheduledWake):
            await self._on_tick(wake)
            return
        if isinstance(wake, SalienceWake):
            # Declared but unused in Phase 1. PR 3b wires the consumer.
            logger.debug(
                "agent.event_loop.salience_wake_ignored: agent_id=%s",
                self._agent_id,
            )
            return
        logger.warning(
            "agent.event_loop.unknown_wake: agent_id=%s type=%s",
            self._agent_id, type(wake).__name__,
        )


# ─── Internal sentinel ──────────────────────────────────────────────────────


class _StopSentinel(WakeEvent):
    """Internal poison-pill enqueued by :meth:`EventLoop.stop`."""


def _wake_kind(wake: WakeEvent) -> str:
    if isinstance(wake, InboundEventWake):
        return "inbound"
    if isinstance(wake, ScheduledWake):
        return "scheduled"
    if isinstance(wake, SalienceWake):
        return "salience"
    return "unknown"
