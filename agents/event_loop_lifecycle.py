"""EventLoop lifecycle-safety helpers — split out of ``agents.event_loop``.

Contains the four methods that defend against three latent defects
surfaced by the RFC 0024 PR 1–4 review pass (PR 5):

* **Reentrancy** (:meth:`_in_supervisor`, :meth:`_spawn_reentrant`) —
  a handle-bearing :class:`InboundEventWake` enqueued from inside the
  loop's own supervisor task runs ``on_event`` on a detached resolver
  task instead of the FIFO the blocked supervisor cannot drain.

* **Queue-full log throttle** (:meth:`_log_queue_full`) — the per-drop
  WARNING is rate-limited to once per
  :attr:`EventLoop._QUEUE_FULL_LOG_INTERVAL_S` so steady-state channel
  backpressure does not flood operator logs.

* **Stop-time handle drain** (:meth:`_drain_pending_handles`) — wakes
  queued behind the in-flight one when ``stop()`` fired have their
  handles resolved with the empty discard result rather than being
  orphaned.

:class:`EventLoop` inherits from :class:`_EventLoopLifecycleMixin`;
the mixin reads instance attributes set by :class:`EventLoop.__init__`:

* ``self._task`` — the supervisor :class:`asyncio.Task`.
* ``self._agent_id`` — for structured log fields.
* ``self._on_event`` — the per-event callback invoked by the reentrant
  resolver task.
* ``self._reentrant_tasks`` — strong-ref set for detached resolver tasks.
* ``self._dropped_count`` — cumulative drop counter (informational).
* ``self._last_queue_full_log``, ``self._monotonic`` — rate-limit state.
* ``self._QUEUE_FULL_LOG_INTERVAL_S`` — throttle window.
* ``self._queue`` — the wake queue, drained by ``_drain_pending_handles``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from .event_loop_types import InboundEventWake
from .observability._metrics_wakes import wake_attrs
from .observability.metrics import try_get_instruments

if TYPE_CHECKING:
    from .event_loop_types import SyncDispatchHandle, WakeEvent
    from .persona_types import AgentAction, AgentEvent

# Use the same logger name as the parent module so existing log-filter
# patterns and caplog assertions remain unchanged.
logger = logging.getLogger("agents.event_loop")

__all__ = ["_EventLoopLifecycleMixin"]


class _EventLoopLifecycleMixin:
    """Mixin holding the lifecycle-safety helpers for :class:`EventLoop`.

    Inherited by :class:`agents.event_loop.EventLoop`; not instantiable
    on its own.  See module docstring for the attributes the mixin reads
    from the consuming class.
    """

    # Declared for mypy's MRO walk — the consuming EventLoop owns the values.
    _task: asyncio.Task[None] | None
    _agent_id: str
    _reentrant_tasks: set[asyncio.Task[None]]
    _dropped_count: int
    _last_queue_full_log: float | None
    _monotonic: Callable[[], float]
    _QUEUE_FULL_LOG_INTERVAL_S: float
    _queue: asyncio.Queue[WakeEvent]

    if TYPE_CHECKING:
        _on_event: Callable[[AgentEvent], Awaitable[list[AgentAction]]]

    def _in_supervisor(self) -> bool:
        """True when the caller runs on this loop's own supervisor task.

        A reentrant enqueue (``on_event`` / ``on_inbound`` re-dispatching to
        the same agent) is detected here so a handle-bearing wake routes onto
        a detached resolver task instead of the FIFO the blocked supervisor
        cannot drain. (PR 1 review finding #1.)
        """
        try:
            return asyncio.current_task() is self._task
        except RuntimeError:
            # No running loop — not a reentrant call by definition.
            return False

    def _spawn_reentrant(
        self, event: AgentEvent, handle: SyncDispatchHandle,
    ) -> None:
        """Run a reentrant handle-bearing inbound wake on a detached task.

        The supervisor is blocked awaiting ``handle``, so resolve it from a
        separate task that runs ``on_event`` directly. The per-agent lock is
        free in the only production reentrancy path (``on_inbound``'s
        ``execute()`` cascading back to the same agent, *after* ``on_event``
        already returned and released the lock), so this does not deadlock on
        the lock; recursion is bounded by the caller's cascade-depth guard.
        On cancellation (loop stopping mid-dispatch) the handle is settled
        with the discard-contract empty result so the awaiter never hangs.

        Observability parity with the FIFO ``_handle_wake`` path: the
        ``agent.wake.inbound`` counter is recorded here (the reentrant wake
        never reaches ``_handle_wake``), and a raising ``on_event`` is logged
        as ``agent.event_loop.wake_failed`` with a traceback before the
        handle is rejected — without it a reentrant failure would be rejected
        silently, unlike the supervised path.

        Single-level only: the detached task runs ``on_event`` (decide), not
        ``on_inbound`` (decide → execute), so it does not itself re-dispatch.
        A reentrant enqueue raised from *inside* this detached task would see
        ``current_task() is not self._task`` (:meth:`_in_supervisor` returns
        ``False``) and land on the FIFO the blocked supervisor cannot drain —
        a deadlock the cascade-depth guard does not prevent. The escape hatch
        is therefore correct only while the detached body stays decide-only.
        """
        inst = try_get_instruments()
        if inst is not None:
            inst.wake_inbound.add(
                1,
                attributes=wake_attrs(
                    agent_id=self._agent_id, wake_kind="inbound",
                ),
            )

        async def _resolve() -> None:
            try:
                actions = await self._on_event(event)
            except asyncio.CancelledError:
                handle.resolve([])
                raise
            except Exception as exc:  # noqa: BLE001 — mirror supervisor reject
                # Mirror ``_handle_wake_supervised``: log with a traceback so
                # a reentrant cascade failure is not swallowed, then reject.
                logger.error(
                    "agent.event_loop.wake_failed: agent_id=%s wake_kind=%s err=%r",
                    self._agent_id, "inbound", exc,
                    exc_info=True,
                )
                handle.reject(exc)
            else:
                handle.resolve(actions)

        task = asyncio.create_task(
            _resolve(), name=f"event-loop-reentrant-{self._agent_id}",
        )
        self._reentrant_tasks.add(task)
        task.add_done_callback(self._reentrant_tasks.discard)

    def _log_queue_full(self) -> None:
        """Rate-limited WARNING for queue-full drops (PR 1 review finding #2).

        RFC 0024 Phase 4 made channel-message drops a steady-state
        backpressure signal, so the original per-drop WARNING flooded
        operator logs. Emit at most once per
        :attr:`_QUEUE_FULL_LOG_INTERVAL_S`; each line carries the cumulative
        :attr:`dropped_count` so the drop rate stays visible. The per-drop
        signal lives on the ``agent.wake.dropped`` OTEL counter and
        ``dropped_count``, both of which move on every drop regardless.
        """
        now = self._monotonic()
        last = self._last_queue_full_log
        if last is not None and (now - last) < self._QUEUE_FULL_LOG_INTERVAL_S:
            return
        logger.warning(
            "agent.event_loop.queue_full: agent_id=%s dropped_total=%d "
            "(further drops within %.0fs suppressed)",
            self._agent_id, self._dropped_count, self._QUEUE_FULL_LOG_INTERVAL_S,
        )
        self._last_queue_full_log = now

    def _drain_pending_handles(self) -> None:
        """Settle handle-bearing wakes still queued at :meth:`stop` time.

        Drains the queue non-blockingly and resolves each pending
        :class:`InboundEventWake` handle with the empty action list — the
        same discard-contract result a queue-full drop yields. Without this,
        a wake enqueued behind the in-flight one when ``stop()`` fired would
        orphan its handle and the awaiting caller would hang to its external
        ``wait_for`` deadline. (PR 1 review finding #5.)
        """
        while True:
            try:
                wake = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if (
                isinstance(wake, InboundEventWake)
                and wake.handle is not None
                and not wake.handle.done()
            ):
                wake.handle.resolve([])
