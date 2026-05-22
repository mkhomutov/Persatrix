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

Wake taxonomy, :class:`SyncDispatchHandle`, and the lifecycle-safety
helpers live in sibling modules split for file-size review-friendliness
(RFC 0024 PR 5):

* :mod:`agents.event_loop_types` — :class:`WakeEvent`, :class:`InboundEventWake`,
  :class:`ScheduledWake`, :class:`SalienceWake`, :class:`SyncDispatchHandle`.
* :mod:`agents.event_loop_lifecycle` — reentrancy escape hatch,
  stop-drain, queue-full log throttle.
* :mod:`agents.event_loop_timers` — periodic/one-shot timer registry.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from .event_loop_lifecycle import _EventLoopLifecycleMixin
from .event_loop_salience import _SalienceSubscriber
from .event_loop_timers import _EventLoopTimersMixin, _TimerEntry
from .event_loop_types import (
    InboundEventWake,
    SalienceWake,
    ScheduledWake,
    SyncDispatchHandle,
    WakeEvent,
)
from .memory._events import MemoryWriteBus, get_memory_write_bus
from .observability._metrics_wakes import wake_attrs
from .observability.metrics import try_get_instruments

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


class EventLoop(_EventLoopTimersMixin, _EventLoopLifecycleMixin):
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

    # Rate-limit window for the queue-full WARNING. The per-drop signal
    # lives on the ``agent.wake.dropped`` OTEL counter + :attr:`dropped_count`;
    # the log line is throttled so steady-state channel backpressure
    # (RFC 0024 Phase 4, where channel-message drops are a normal
    # backpressure outcome) cannot flood operator logs. (PR 1 review #2.)
    _QUEUE_FULL_LOG_INTERVAL_S: float = 60.0

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
        # Strong-ref anchor for detached resolver tasks that run reentrant
        # handle-bearing inbound wakes (PR 1 review finding #1) — Python
        # 3.11+ GCs weakly-held tasks mid-flight. Tasks add themselves on
        # creation and a done-callback discards them on completion.
        self._reentrant_tasks: set[asyncio.Task[None]] = set()
        # Rate-limit state for the queue-full WARNING (PR 1 review finding
        # #2). ``_monotonic`` is an injectable seam so tests can drive the
        # throttle window without real wall-clock sleeps.
        self._last_queue_full_log: float | None = None
        self._monotonic: Callable[[], float] = time.monotonic
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

        Two correctness guards ride on this method (RFC 0024 PR 5):

        * **Post-stop TOCTOU** — once :meth:`stop` has set ``_stopped`` no
          producer's wake can ever drain, so reject immediately and settle
          any handle with the empty-action discard result rather than let
          the caller hang on its external ``wait_for`` deadline. Closes the
          window where a producer checks :attr:`is_running` and ``enqueue``\\ s
          while ``stop()`` races in between. (PR 1 review finding #5.)
        * **Reentrancy** — a handle-bearing :class:`InboundEventWake`
          enqueued from inside this loop's own supervisor task (e.g.
          ``on_inbound``'s ``execute()`` cascading back to the same agent)
          cannot be drained by the FIFO — the single supervisor is blocked
          awaiting the handle. Run ``on_event`` on a detached resolver task
          instead, so the awaiter resolves instead of FIFO-starving until
          its dispatch-timeout. (PR 1 review finding #1.)
        """
        if self._stopped.is_set():
            if isinstance(wake, InboundEventWake) and wake.handle is not None:
                # Same discard-contract result EventDispatcher.dispatch
                # returns for a queue-full / cascade / unknown-target drop.
                wake.handle.resolve([])
            return False

        if (
            isinstance(wake, InboundEventWake)
            and wake.handle is not None
            and self._in_supervisor()
        ):
            self._spawn_reentrant(wake.event, wake.handle)
            return True

        try:
            self._queue.put_nowait(wake)
        except asyncio.QueueFull:
            self._dropped_count += 1
            self._log_queue_full()
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

        # Abort in-flight reentrant resolvers *before* awaiting the
        # supervisor, and await the cancellations to settle. Two reasons
        # (PR 1 review #5; PR 5 re-review #2):
        #   1. The resolver's cancellation path resolves its handle with the
        #      empty discard result. A supervisor parked on that handle (the
        #      ``on_inbound`` cascade case) then unblocks and drains
        #      gracefully on the next loop iteration instead of waiting out
        #      ``timeout`` and being hard-cancelled — a hard cancel would
        #      cascade into the awaited future and leave the handle
        #      *cancelled* rather than resolved.
        #   2. Awaiting the cancelled tasks means every reentrant handle is
        #      settled by the time ``stop()`` returns, not on a later tick.
        # ``_stopped`` is already set above, so :meth:`enqueue` rejects any
        # further reentrant spawn — this snapshot is complete. The wait is
        # bounded by ``timeout`` (mirroring the supervisor wait below): a
        # well-behaved ``on_event`` settles on cancellation at once, so this
        # returns immediately; the bound only guards a handler that swallows
        # ``CancelledError`` so a pathological resolver cannot wedge stop().
        reentrant = list(self._reentrant_tasks)
        for task in reentrant:
            task.cancel()
        if reentrant:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*reentrant, return_exceptions=True),
                    timeout=timeout,
                )
            except TimeoutError:
                logger.warning(
                    "agent.event_loop.stop_reentrant_timeout: "
                    "agent_id=%s timeout=%.1fs pending=%d",
                    self._agent_id, timeout, len(reentrant),
                )

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
        # Settle handle-bearing wakes still queued behind the in-flight one
        # when ``stop()`` fired; the ``_stopped`` guard in :meth:`enqueue`
        # closes the producer-side window, this closes the already-queued
        # side. (PR 1 review #5.)
        self._drain_pending_handles()
        self._task = None

    # Timer registry methods (``register_timer``, ``unregister_timer``,
    # ``has_timer``, ``_arm_timer``, ``_next_delay``) and the
    # ``_TimerEntry`` dataclass live on :class:`_EventLoopTimersMixin`
    # in :mod:`agents.event_loop_timers` — split for file-size review-
    # friendliness (RFC 0024 PR 2.1, deferred PR 2 review item 2).
    # ``_timers`` remains owned here; the mixin reads it via ``self``.
    #
    # Lifecycle-safety helpers (``_in_supervisor``, ``_spawn_reentrant``,
    # ``_log_queue_full``, ``_drain_pending_handles``) live on
    # :class:`_EventLoopLifecycleMixin` in :mod:`agents.event_loop_lifecycle`
    # — split for file-size review-friendliness (RFC 0024 PR 5).

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
                # decide → execute → recover.
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
            # v0.3.3's default-off invariant means this branch never runs
            # under stock config.  v0.4.0+ consumer tracked separately.
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
