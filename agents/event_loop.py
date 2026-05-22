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
queue rejects new wakes via :meth:`EventLoop.enqueue` returning ``False``,
increments :attr:`EventLoop.dropped_count`, and records the
``agent.wake.dropped`` OTEL counter. As of RFC 0024 Phase 4
channel-message dispatch enqueues fire-and-forget
:class:`InboundEventWake`\\ s directly (``EventDispatcher.enqueue_inbound``
→ this queue), making the channel surface the dominant producer the
discard policy defends against.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Generator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .event_loop_salience import _SalienceSubscriber
from .event_loop_timers import _EventLoopTimersMixin, _TimerEntry
from .memory._events import MemoryWriteBus, get_memory_write_bus
from .observability._metrics_wakes import wake_attrs
from .observability.metrics import try_get_instruments

if TYPE_CHECKING:
    from .memory._events import MemoryWriteEvent
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


# ─── EventLoop ──────────────────────────────────────────────────────────────


class EventLoop(_EventLoopTimersMixin):
    """Per-agent wake queue + supervisor.

    Callback-driven so :class:`agents.tick.TickScheduler` can wire its idle
    accounting and the existing ``ActionExecutor`` plumbing without the
    loop importing the persona surface.

    Inbound wakes split on handle presence (RFC 0024 Phase 4): a
    handle-bearing wake (chat / cascade) runs ``on_event`` and resolves
    the handle with its actions — the synchronous caller owns execution.
    A handle-less wake (channel message) runs ``on_inbound``, which owns
    the whole lifecycle (decide → execute → recover); the loop falls back
    to ``on_event`` (discarding actions) when ``on_inbound`` is not wired.
    ``on_tick`` (for :class:`ScheduledWake`) returns nothing — scheduled
    wakes are fire-and-forget and the adapter executes its own actions.
    """

    _DEFAULT_QUEUE_SIZE = 1024

    # RFC 0024 §Security Considerations busy-loop guard floor; mirrors
    # ``TickScheduler._MIN_INTERVAL`` and the schema's ``minimum: 1.0``
    # on ``autonomy.timers[*].interval_seconds``.  Defense-in-depth for
    # any programmatic caller bypassing schema validation.
    _MIN_INTERVAL: float = 1.0

    # RFC 0024 PR 3b: default ``autonomy.salience_threshold`` and
    # ``autonomy.salience_rate_max_per_sec``.  The threshold default is
    # strictly above PR 3a's :data:`REFLECTION_CONTRADICTION_SALIENCE`
    # (``0.6``) so salience wakes stay off by inequality under stock
    # scoring — the inequality is the v0.3.3 "Idle Truly Idle"
    # release-gate invariant, pinned by
    # ``test_event_loop_salience_default_off``.
    DEFAULT_SALIENCE_THRESHOLD: float = 0.95
    DEFAULT_SALIENCE_RATE_MAX_PER_SEC: int = 10

    def __init__(
        self,
        *,
        agent_id: str,
        on_event: Callable[[AgentEvent], Awaitable[list[AgentAction]]],
        on_tick: Callable[[ScheduledWake], Awaitable[None]],
        on_inbound: Callable[[AgentEvent], Awaitable[None]] | None = None,
        queue_size: int = _DEFAULT_QUEUE_SIZE,
        salience_threshold: float | None = None,
        salience_rate_max_per_sec: int | None = None,
        salience_time_fn: Callable[[], float] | None = None,
    ) -> None:
        self._agent_id = agent_id
        self._on_event = on_event
        self._on_tick = on_tick
        self._on_inbound = on_inbound
        self._queue: asyncio.Queue[WakeEvent] = asyncio.Queue(maxsize=queue_size)
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()
        self._timers: dict[str, _TimerEntry] = {}
        self._dropped_count = 0
        # RFC 0024 PR 3b: salience-subscriber wiring.  Constructed here so
        # the threshold + rate cap are immutable for the loop's lifetime;
        # subscribed at :meth:`start`, unsubscribed at :meth:`stop` so the
        # global bus does not retain a reference to a stopped loop.
        threshold = (
            salience_threshold
            if salience_threshold is not None
            else self.DEFAULT_SALIENCE_THRESHOLD
        )
        rate_max = (
            salience_rate_max_per_sec
            if salience_rate_max_per_sec is not None
            else self.DEFAULT_SALIENCE_RATE_MAX_PER_SEC
        )
        subscriber_kwargs: dict[str, Any] = {
            "agent_id": agent_id,
            "enqueue": self.enqueue,
            "threshold": threshold,
            "rate_max_per_sec": rate_max,
        }
        if salience_time_fn is not None:
            subscriber_kwargs["time_fn"] = salience_time_fn
        self._salience_subscriber = _SalienceSubscriber(**subscriber_kwargs)
        self._subscribed_bus: MemoryWriteBus | None = None

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

    # ─── enqueue / drain ───────────────────────────────────────────────

    def enqueue(self, wake: WakeEvent) -> bool:
        """Enqueue a wake event. Returns ``True`` if accepted, ``False`` if dropped.

        Discard-not-block per RFC 0024 Decided §1. PR 3b wires the
        ``agent.wake.dropped`` OTEL counter alongside :attr:`dropped_count`
        so dashboards and the substrate's internal counter agree.
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
            inst = try_get_instruments()
            if inst is not None:
                inst.wake_dropped.add(
                    1,
                    attributes=wake_attrs(
                        agent_id=self._agent_id, wake_kind="dropped",
                    ),
                )
            return False
        return True

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopped.clear()
        # Subscribe to the memory-write bus exactly once per
        # start/stop cycle.  Idempotent: if already subscribed (no stop
        # in between), skip the re-subscribe so the bus subscriber list
        # cannot accumulate duplicates.
        if self._subscribed_bus is None:
            bus = get_memory_write_bus()
            bus.subscribe(self._salience_subscriber)
            self._subscribed_bus = bus
        self._task = asyncio.create_task(
            self._run(), name=f"event-loop-{self._agent_id}",
        )

    async def stop(self, timeout: float = 10.0) -> None:
        """Cancel timers and wait for the loop to drain in-flight wakes."""
        self._stopped.set()
        # Unsubscribe first so further publishes do not enqueue wakes
        # onto a queue we are about to drain.  ``unsubscribe`` is
        # idempotent — safe to call without a matching subscribe.
        if self._subscribed_bus is not None:
            self._subscribed_bus.unsubscribe(self._salience_subscriber)
            self._subscribed_bus = None
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

    # Timer registry methods (``register_timer``, ``unregister_timer``,
    # ``has_timer``, ``_arm_timer``, ``_next_delay``) and the
    # ``_TimerEntry`` dataclass live on :class:`_EventLoopTimersMixin`
    # in :mod:`agents.event_loop_timers` — split for file-size review-
    # friendliness (RFC 0024 PR 2.1, deferred PR 2 review item 2).
    # ``_timers`` remains owned here; the mixin reads it via ``self``.

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
            inst = try_get_instruments()
            if inst is not None:
                inst.wake_inbound.add(
                    1,
                    attributes=wake_attrs(
                        agent_id=self._agent_id, wake_kind="inbound",
                    ),
                )
            if wake.handle is not None:
                # Synchronous-reply path (chat / in-process cascade): the
                # caller awaits the handle and owns execution.
                actions = await self._on_event(wake.event)
                wake.handle.resolve(actions)
                return
            if self._on_inbound is not None:
                # Fire-and-forget path (channel messages): the loop owns
                # decide → execute → recover.  ``on_inbound`` recovers the
                # modelled wallet/rate-limit classes internally; anything
                # else falls to ``_handle_wake_supervised`` (no handle to
                # reject).
                await self._on_inbound(wake.event)
                return
            # No fire-and-forget handler wired (generic test loops): run
            # the agent and discard the actions.
            await self._on_event(wake.event)
            return
        if isinstance(wake, ScheduledWake):
            inst = try_get_instruments()
            if inst is not None:
                inst.wake_scheduled.add(
                    1,
                    attributes=wake_attrs(
                        agent_id=self._agent_id,
                        wake_kind="scheduled",
                        timer_id=wake.timer_id,
                    ),
                )
            await self._on_tick(wake)
            return
        if isinstance(wake, SalienceWake):
            # PR 3b: the salience-wake counter is recorded at the
            # subscriber call site (with ``suppressed_reason``).  The
            # consumer (a wake that successfully enqueued and is now
            # draining) is intentionally a structured-log breadcrumb;
            # the "on_event for the triggering write" behaviour the
            # RFC §G text names is a v0.4.0+ consumer (RFC 0027
            # reflection-driven consolidation).  v0.3.3's default-off
            # invariant means this branch never runs under stock config.
            logger.info(
                "agent.event_loop.salience_wake: agent_id=%s tier=%s salience=%.3f",
                self._agent_id,
                getattr(wake.write_event, "tier", "<unknown>"),
                getattr(wake.write_event, "salience", -1.0),
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
