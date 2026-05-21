"""
Persatrix Tick Scheduler — thin adapter over :class:`agents.event_loop.EventLoop`.

RFC 0024 Phase 1: the autonomous polling loop is gone. ``TickScheduler``
now constructs an :class:`EventLoop` and registers a single periodic timer
(``ScheduledWake(timer_id="legacy_tick", callback_kind="tick")``) at the
configured ``interval``. Idle accounting, energy recovery, and executor
wiring live in the adapter's ``on_tick`` callback so existing tests
(``tests/unit/python/test_tick_scheduler.py``) keep their assertions.

Back-compat contract:

* ``tick_interval_seconds`` in ``agents.yaml`` continues to work — Phase 2
  introduces ``autonomy.timers`` and Phase 5 / v0.4.0 emits the deprecation
  warning.
* ``wake()`` resets ``idle_count`` and enqueues a single ``ScheduledWake``
  so woken agents fire on_tick immediately instead of waiting the rest of
  the interval — preserves the v0.3.2 "event arrived, tick now" semantics
  for fire-and-forget callers.
* ``EventDispatcher.dispatch()`` no longer calls ``wake()``; it goes through
  ``event_loop.enqueue(InboundEventWake(..., handle=...))`` directly. The
  ``wake()`` method stays as a public API for any external fire-and-forget
  caller (currently none in tree).

Lock protocol
~~~~~~~~~~~~~
Unchanged from v0.3.2:

- **Idle branch** (``is_idle`` is True): acquires ``agent.exclusive()``
  explicitly because ``recover_idle_energy()`` does *not* acquire the lock
  internally.
- **Non-idle branch**: calls ``agent.on_tick()`` which acquires the
  per-agent lock *internally*.  Wrapping in ``exclusive()`` here would
  deadlock because ``asyncio.Lock`` is not reentrant.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .cascade_depth_defaults import DEFAULT_MAX_CASCADE_DEPTH
from .event_loop import EventLoop, ScheduledWake
from .persona_types import ActionType

if TYPE_CHECKING:
    from .dispatch import ActionExecutor
    from .event_loop import InboundEventWake  # noqa: F401 — typing context only
    from .persona_runtime import _LLMPersonaAgent
    from .persona_types import AgentAction, AgentEvent

logger = logging.getLogger(__name__)

__all__ = ["TickScheduler"]


_LEGACY_TIMER_ID = "legacy_tick"


class TickScheduler:
    """Thin adapter over :class:`EventLoop` for autonomous persona agents.

    Registers a single legacy timer that synthesises ``ScheduledWake`` at
    ``interval``. The adapter's ``on_tick`` callback runs the idle-detection
    + energy-recovery + executor logic that lived inline in v0.3.2.
    """

    # Minimum tick interval to prevent accidental busy loops from
    # zero or negative configuration values.  Set to 1.0s (not lower)
    # because each tick triggers an LLM call; at 0.01s a misconfigured
    # agent would fire 100 calls/second during the initial non-idle
    # window, incurring significant cost before idle detection kicks in.
    # (F-64-DR2-11: 10ms floor allows cost-burst from misconfigured agents.)
    _MIN_INTERVAL: float = 1.0

    def __init__(
        self,
        agent: _LLMPersonaAgent,
        *,
        interval: float = 60.0,
        max_actions_per_tick: int = 3,
        idle_after_ticks: int = 10,
        executor: ActionExecutor | None = None,
        register_legacy_timer: bool = True,
        salience_threshold: float | None = None,
        salience_rate_max_per_sec: int | None = None,
    ) -> None:
        self._agent = agent
        if interval < self._MIN_INTERVAL:
            logger.warning(
                "Tick interval %.2fs below minimum, clamping to %.1fs",
                interval,
                self._MIN_INTERVAL,
            )
            interval = self._MIN_INTERVAL
        self._interval = interval
        self._max_actions_per_tick = max_actions_per_tick
        self._idle_after_ticks = idle_after_ticks
        self._executor = executor
        self._idle_count = 0
        # RFC 0024 PR 2: when ``autonomy.timers`` is configured the
        # caller registers timers directly on the EventLoop and the
        # synthesised legacy timer is skipped — ``start()`` checks this
        # flag.  Default ``True`` preserves PR 1 back-compat for any
        # call site (tests, external callers) that constructs a
        # TickScheduler without going through ``initialize_persona_agents``.
        self._register_legacy_timer = register_legacy_timer
        # RFC 0024 PR 3b: forward the salience knobs onto the EventLoop
        # ctor so a deployed persona's ``autonomy.salience_threshold`` /
        # ``autonomy.salience_rate_max_per_sec`` override the class-level
        # defaults.  ``None`` leaves the EventLoop's documented defaults
        # in place (threshold ``0.95`` strictly above PR 3a's max
        # scoring; rate cap ``10/sec`` per RFC §Security Considerations).
        self._event_loop = EventLoop(
            agent_id=agent.agent_id,
            on_event=self._handle_event_wake,
            on_tick=self._handle_scheduled_wake,
            salience_threshold=salience_threshold,
            salience_rate_max_per_sec=salience_rate_max_per_sec,
        )

    # ─── public surface (preserved from v0.3.2) ────────────────────────

    @property
    def event_loop(self) -> EventLoop:
        """The underlying :class:`EventLoop` — exposed to
        :class:`EventDispatcher` so it can enqueue
        :class:`InboundEventWake` with a :class:`SyncDispatchHandle`."""
        return self._event_loop

    @property
    def idle_count(self) -> int:
        """Number of consecutive idle ticks."""
        return self._idle_count

    @property
    def is_idle(self) -> bool:
        """Whether the scheduler has exceeded idle_after_ticks threshold."""
        return self._idle_count >= self._idle_after_ticks

    @property
    def is_running(self) -> bool:
        """Whether the underlying :class:`EventLoop` task is active."""
        return self._event_loop.is_running

    @property
    def _task(self) -> object | None:
        """v0.3.2 back-compat shim — pre-refactor tests reach into ``_task``
        directly to verify start-idempotency.  Returns the underlying
        :class:`EventLoop`'s supervisor task via its public ``task``
        property (or ``None`` when the loop has not been started yet)."""
        return self._event_loop.task

    def wake(self) -> None:
        """Reset idle state and fire ``on_tick`` immediately.

        v0.3.2 semantics preserved: external fire-and-forget callers that
        signal "you have new work" get an immediate tick.
        ``EventDispatcher.dispatch`` no longer calls this on the hot path
        — it enqueues an :class:`InboundEventWake` with a handle directly
        — but the dispatcher still falls back to ``wake()`` when a
        scheduler is registered without being started (test fixtures).

        Only enqueues a ScheduledWake when the underlying :class:`EventLoop`
        is running; otherwise the wake's only observable effect is the
        idle-counter reset (the test-fixture path).
        """
        self._idle_count = 0
        if self._event_loop.is_running:
            self._event_loop.enqueue(
                ScheduledWake(timer_id=_LEGACY_TIMER_ID, callback_kind="tick"),
            )

    def start(self) -> None:
        """Start the underlying :class:`EventLoop` and register the legacy timer.

        When ``register_legacy_timer=False`` (RFC 0024 PR 2: caller is
        using ``autonomy.timers``), only the supervisor is started —
        the synthesised back-compat timer is suppressed and the caller
        is responsible for registering any timers it needs via
        :attr:`event_loop`.
        """
        if self._event_loop.is_running:
            return
        self._event_loop.start()
        if not self._register_legacy_timer:
            return
        # Register only once across start/stop/start cycles — _MIN_INTERVAL
        # has already clamped the interval at __init__ time.  Uses the
        # public ``has_timer`` API so this adapter does not reach into
        # ``EventLoop._timers`` private state.
        if not self._event_loop.has_timer(_LEGACY_TIMER_ID):
            self._event_loop.register_timer(
                timer_id=_LEGACY_TIMER_ID,
                callback_kind="tick",
                interval=self._interval,
            )

    async def stop(self, timeout: float = 10.0) -> None:
        """Stop the underlying :class:`EventLoop` and cancel the legacy timer."""
        await self._event_loop.stop(timeout=timeout)

    # ─── callbacks (the v0.3.2 _run() body lives here now) ─────────────

    async def _handle_scheduled_wake(self, wake: ScheduledWake) -> None:
        """Handle one ``ScheduledWake`` — preserves the v0.3.2 tick semantics.

        Idle branch acquires ``agent.exclusive()`` explicitly because
        ``recover_idle_energy()`` does NOT acquire the lock internally.
        Non-idle branch calls ``on_tick()`` which acquires the per-agent
        lock internally — wrapping here would deadlock (asyncio.Lock is
        not reentrant).
        """
        if self.is_idle:
            logger.debug(
                "Agent %s idle (%d ticks), skipping LLM tick",
                self._agent.agent_id,
                self._idle_count,
            )
            async with self._agent.exclusive():
                self._agent.recover_idle_energy()
            return

        # on_tick() acquires self._agent._lock internally — do NOT wrap
        # in self._agent.exclusive() (asyncio.Lock is not reentrant).
        actions = await self._agent.on_tick()
        actions = actions[: self._max_actions_per_tick]

        all_do_nothing = all(
            a.action_type == ActionType.DO_NOTHING for a in actions
        )
        if all_do_nothing:
            self._idle_count += 1
        else:
            self._idle_count = 0

        # cascade_depth=DEFAULT_MAX_CASCADE_DEPTH explicitly: the tick loop
        # has no inbound event to derive depth from (unlike
        # EventDispatcher.dispatch which forwards inbound_depth + 1), so any
        # SEND_CHANNEL_MESSAGE an on_tick produces must publish at the cap so
        # the orchestrator's cascade_depth >= max_cascade_depth clamp drops
        # fan-out. The v0.3.0 demo runaway cascade was the consequence of
        # the previous publish-at-depth-0 default.
        if self._executor is not None:
            await self._executor.execute(
                self._agent.agent_id, actions,
                cascade_depth=DEFAULT_MAX_CASCADE_DEPTH,
            )
        elif not all_do_nothing:
            # No executor configured but agent produced actionable output —
            # likely a wiring bug.  Log so operators can diagnose silent
            # action discard (F-64-DR-14).
            logger.warning(
                "Agent %s produced %d non-idle action(s) but no executor "
                "is configured — actions discarded",
                self._agent.agent_id,
                len(actions),
            )

    async def _handle_event_wake(self, event: AgentEvent) -> list[AgentAction]:
        """Dispatch an inbound event — reached when
        :class:`EventDispatcher` enqueues an :class:`InboundEventWake` on
        this agent's loop.

        Resets ``idle_count`` so a woken autonomous agent does not stay in
        the idle branch.  Returns the agent's actions so the dispatch
        handle resolves with the right ``list[AgentAction]``.
        """
        self._idle_count = 0
        return await self._agent.on_event(event)
