"""Per-agent timer registry — split out of ``agents.event_loop`` for
file-size review-friendliness (RFC 0024 PR 2.1, deferred follow-up (2)
from the PR 2 review pass).

The :class:`EventLoop` exposes the public timer surface
(:meth:`EventLoop.register_timer`, :meth:`EventLoop.unregister_timer`,
:meth:`EventLoop.has_timer`).  The implementation of those methods and
the internal :class:`_TimerEntry` dataclass live here; :class:`EventLoop`
inherits from :class:`_EventLoopTimersMixin` so callers see no surface
change.  ``EventLoop._timers`` remains the single owner of timer state.

The mixin reads three attributes set by :class:`EventLoop.__init__`:

* ``self._timers`` — the timer-id → :class:`_TimerEntry` registry.
* ``self._MIN_INTERVAL`` — the busy-loop floor (RFC 0024 §Security
  Considerations).
* ``self.enqueue`` — the wake-queue producer used by ``_fire``.

These are typed as class attributes on :class:`EventLoop` so mypy walks
the MRO when checking the mixin's method bodies.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .event_loop import WakeEvent

__all__ = ["_EventLoopTimersMixin", "_TimerEntry"]


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


class _EventLoopTimersMixin:
    """Mixin holding the periodic/one-shot timer registry methods.

    Inherited by :class:`agents.event_loop.EventLoop`; not instantiable
    on its own.  Method bodies reference ``self._timers``,
    ``self._MIN_INTERVAL``, and ``self.enqueue`` — declared on the
    consuming :class:`EventLoop`.
    """

    # Declared here for mypy's benefit; the consuming class owns the
    # actual values (set in ``__init__`` / declared as class attribute).
    _timers: dict[str, _TimerEntry]
    _MIN_INTERVAL: float

    if TYPE_CHECKING:
        # ``self.enqueue`` is called inside ``_arm_timer._fire``; the
        # real implementation lives on :class:`agents.event_loop.EventLoop`.
        # This declaration exists only for static analysis so mypy can
        # resolve the attribute on the mixin's own surface — kept under
        # ``TYPE_CHECKING`` so no runtime method shadows the MRO lookup.
        # (RFC 0024 PR 2.1 review follow-up: an earlier
        # ``raise NotImplementedError`` body added a failure cliff for any
        # future MRO accident that pinned the stub instead of
        # :meth:`EventLoop.enqueue`; pinned by
        # ``test_event_loop_timers.TestMixinEnqueueIsTypeOnly``.)
        def enqueue(self, wake: WakeEvent) -> bool: ...

    def has_timer(self, timer_id: str) -> bool:
        """Whether ``timer_id`` is currently registered.

        Public encapsulation boundary for :class:`agents.tick.TickScheduler`
        and adapters that need idempotent re-registration without reaching
        into :attr:`_timers`.
        """
        return timer_id in self._timers

    def register_timer(
        self,
        *,
        timer_id: str,
        callback_kind: str,
        interval: float | None = None,
        jitter_max: float = 0.0,
        fire_after: float | None = None,
        initial_delay: float | None = None,
    ) -> None:
        """Register a periodic (``interval``) or one-shot (``fire_after``)
        :class:`ScheduledWake` producer.  Periodic re-arm is monotonic via
        ``call_later`` (RFC 0024 §C); one-shot self-cleans after firing
        (no ``_MIN_INTERVAL`` floor — cannot busy-loop).  ``jitter_max``
        randomises each re-arm by ``±jitter_max`` seconds, capped at
        ``interval - _MIN_INTERVAL`` so draws stay above the busy-loop
        floor.

        ``initial_delay`` (periodic only — RFC 0024 PR 2.1) overrides the
        first-fire delay so a persona restarted mid-jitter-window resumes
        at its saved :attr:`ScheduledWakeRow.next_fire_at_ms` anchor.
        Subsequent re-arms still use :meth:`_next_delay`.  Caller (the
        :class:`ScheduledWakesCache` loader) is responsible for clamping
        the value into a sensible range; the API enforces
        ``initial_delay >= 0`` as the minimal contract.

        Raises ``ValueError`` on any constraint violation.
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
        if jitter_max < 0.0:
            raise ValueError(f"jitter_max {jitter_max}s must be non-negative")
        if interval is not None and jitter_max > (slack := interval - self._MIN_INTERVAL):
            raise ValueError(f"jitter_max {jitter_max}s exceeds slack {slack}s")
        if initial_delay is not None:
            if fire_after is not None:
                raise ValueError(
                    "initial_delay is for periodic timers; one-shot "
                    "carries its delay in fire_after",
                )
            if initial_delay < 0.0:
                raise ValueError(
                    f"initial_delay {initial_delay}s must be non-negative",
                )

        entry = _TimerEntry(
            interval=interval,
            callback_kind=callback_kind,
            jitter_max=jitter_max,
            one_shot=fire_after is not None,
        )
        self._timers[timer_id] = entry
        # For one-shot timers ``fire_after`` is the initial delay; for
        # periodic timers ``initial_delay`` overrides the default
        # ``_next_delay`` first-fire when set (restart-mid-jitter-window).
        first_arm_delay = fire_after if fire_after is not None else initial_delay
        self._arm_timer(timer_id, entry, initial_delay=first_arm_delay)

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
        from .event_loop import ScheduledWake  # local: avoid import cycle

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
        """Periodic re-arm delay in ``[interval-jitter_max, interval+jitter_max]``
        (``jitter_max=0.0`` returns ``entry.interval`` deterministically).
        :meth:`register_timer` caps ``jitter_max`` so the lower bound stays
        at/above ``_MIN_INTERVAL``.  Explicit raise survives ``python -O``."""
        if entry.interval is None:
            raise RuntimeError("_next_delay called on a one-shot timer entry")
        if entry.jitter_max <= 0.0:
            return entry.interval
        return entry.interval + random.uniform(-entry.jitter_max, entry.jitter_max)
