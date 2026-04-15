"""
Persatrix Tick Scheduler.

Autonomous tick loop for persona agents with idle detection.
Extracted from ``persona.py`` for modularity — no logic changes.

Lock protocol
~~~~~~~~~~~~~
The tick loop has two branches with different locking strategies:

- **Idle branch** (``is_idle`` is True): acquires ``agent.exclusive()``
  explicitly because ``recover_idle_energy()`` does *not* acquire the
  lock internally.
- **Non-idle branch**: calls ``agent.on_tick()`` which acquires the
  per-agent lock *internally*.  Wrapping in ``exclusive()`` here would
  deadlock because ``asyncio.Lock`` is not reentrant.

This asymmetry is intentional and must be preserved if the lock
strategy changes.
(F-64-DR2-13: document lock protocol in module docstring.)
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from .persona_types import ActionType

if TYPE_CHECKING:
    from .dispatch import ActionExecutor
    from .persona_runtime import _LLMPersonaAgent

logger = logging.getLogger(__name__)

__all__ = ["TickScheduler"]


class TickScheduler:
    """Autonomous tick loop for persona agents.

    Fires ``on_tick()`` at configurable intervals. Tracks idle ticks
    (consecutive ``DO_NOTHING`` actions) and skips LLM calls when idle.
    Supports ``wake()`` to reset idle state on incoming events.
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
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()
        self._wake_event = asyncio.Event()

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
        """Whether the tick loop task is active."""
        return self._task is not None and not self._task.done()

    def wake(self) -> None:
        """Reset idle state and wake the tick loop.

        Called by EventDispatcher when an event arrives for this agent.
        """
        self._idle_count = 0
        self._wake_event.set()

    def start(self) -> None:
        """Start the tick loop as an asyncio task."""
        if self._task is not None and not self._task.done():
            return
        self._stopped.clear()
        self._task = asyncio.create_task(self._run(), name=f"tick-{self._agent.agent_id}")

    async def stop(self, timeout: float = 10.0) -> None:
        """Stop the tick loop, waiting for in-flight operations.

        Args:
            timeout: Maximum seconds to wait for the current tick to complete.
        """
        self._stopped.set()
        self._wake_event.set()  # Unblock any wait
        if self._task is not None and not self._task.done():
            try:
                # shield() prevents wait_for's cancellation from killing
                # the task — _stopped + _wake_event already signal _run()
                # to exit cleanly.  If it doesn't exit within `timeout`,
                # we cancel the task explicitly in the TimeoutError branch.
                await asyncio.wait_for(asyncio.shield(self._task), timeout=timeout)
            except TimeoutError:
                logger.warning(
                    "Tick scheduler for %s did not stop within %.0fs, cancelling",
                    self._agent.agent_id,
                    timeout,
                )
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
            except asyncio.CancelledError:
                pass
        self._task = None

    async def _run(self) -> None:
        """Main tick loop."""
        logger.info(
            "Tick scheduler started for %s (interval=%.0fs, idle_after=%d)",
            self._agent.agent_id,
            self._interval,
            self._idle_after_ticks,
        )
        while not self._stopped.is_set():
            # Wait for interval or wake signal
            self._wake_event.clear()
            try:
                await asyncio.wait_for(
                    self._wait_for_stop_or_wake(),
                    timeout=self._interval,
                )
                # _stopped or _wake_event was set
                if self._stopped.is_set():
                    break
                # Woken up — fall through to tick immediately
            except TimeoutError:
                # Normal interval elapsed
                pass

            if self._stopped.is_set():
                break

            # Skip LLM calls when idle, but still recover energy so
            # woken agents aren't energy-depleted after long idle periods.
            # Brief lock acquire ensures consistency with concurrent
            # on_event() which may drain_energy().
            # (Review finding: idle energy starvation.)
            if self.is_idle:
                logger.debug(
                    "Agent %s idle (%d ticks), skipping LLM tick",
                    self._agent.agent_id,
                    self._idle_count,
                )
                # Use public API instead of reaching into private
                # attributes (PR #55 review: TickScheduler should use
                # public API for agent lock and state).
                async with self._agent.exclusive():
                    self._agent.recover_idle_energy()
                continue

            try:
                # on_tick() acquires self._agent._lock internally — do NOT
                # wrap this call in self._agent.exclusive().  asyncio.Lock
                # is not reentrant: acquiring here + inside on_tick() would
                # deadlock.  The idle-recovery branch above acquires the
                # lock explicitly because recover_idle_energy() does NOT
                # acquire it internally.
                # (PR #64 review F-64-DR-12: document lock asymmetry.)
                actions = await self._agent.on_tick()
                # Limit actions per tick
                actions = actions[: self._max_actions_per_tick]

                # Track idle state
                all_do_nothing = all(
                    a.action_type == ActionType.DO_NOTHING for a in actions
                )
                if all_do_nothing:
                    self._idle_count += 1
                else:
                    self._idle_count = 0

                # Execute actions
                if self._executor is not None:
                    await self._executor.execute(self._agent.agent_id, actions)
                elif not all_do_nothing:
                    # No executor configured but agent produced actionable
                    # output — likely a wiring bug.  Log so operators can
                    # diagnose why actions are silently discarded.
                    # (PR #64 review F-64-DR-14: silent action discard.)
                    logger.warning(
                        "Agent %s produced %d non-idle action(s) but no "
                        "executor is configured — actions discarded",
                        self._agent.agent_id,
                        len(actions),
                    )

            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Tick error for agent %s", self._agent.agent_id,
                )

        logger.info("Tick scheduler stopped for %s", self._agent.agent_id)

    async def _wait_for_stop_or_wake(self) -> None:
        """Wait until either ``_stopped`` or ``_wake_event`` is set.

        Creates two ``asyncio.Task`` objects — one for each event — and
        uses ``asyncio.wait(return_when=FIRST_COMPLETED)`` to unblock as
        soon as either fires.  The losing task is cancelled in the
        ``finally`` block to avoid leaked coroutines.

        This dual-event pattern avoids race conditions that would arise
        from sequential ``await`` calls (missing the other signal while
        waiting on the first).
        """
        stop_task = asyncio.create_task(self._stopped.wait())
        wake_task = asyncio.create_task(self._wake_event.wait())
        try:
            await asyncio.wait(
                {stop_task, wake_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for t in (stop_task, wake_task):
                if not t.done():
                    t.cancel()
                    try:
                        await t
                    except asyncio.CancelledError:
                        pass
