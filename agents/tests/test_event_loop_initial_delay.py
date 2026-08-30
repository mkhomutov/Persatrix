"""``EventLoop.register_timer(initial_delay=...)`` — RFC 0024 PR 2.1.

PR 2.1 wires the ``ScheduledWakesCache`` into the persona-agents init
path so a persona restarted *mid-jitter-window* resumes its saved
:attr:`ScheduledWakeRow.next_fire_at_ms` anchor instead of firing
immediately.  The mechanism is a new ``initial_delay`` kwarg on
:meth:`EventLoop.register_timer` that overrides the first-fire delay for
a periodic timer.

Tests pin three contract pieces:

* Periodic timer with ``initial_delay`` set → first fire honours that
  delay, subsequent re-arms use :meth:`EventLoop._next_delay`.
* ``initial_delay=None`` (default) preserves the PR 2 first-fire shape
  (no behaviour change for callers that do not opt in).
* Negative ``initial_delay`` raises at the API boundary — defense in
  depth against a clamping bug in the caller.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import pytest

from agents.event_loop import EventLoop, ScheduledWake
from agents.persona_types import AgentAction, AgentEvent


def _build_loop(
    *,
    on_event: Callable[[AgentEvent], Awaitable[list[AgentAction]]] | None = None,
    on_tick: Callable[[ScheduledWake], Awaitable[None]] | None = None,
    agent_id: str = "test-agent",
) -> EventLoop:
    async def _default_event(event: AgentEvent) -> list[AgentAction]:
        return []

    async def _default_tick(wake: ScheduledWake) -> None:
        return None

    return EventLoop(
        agent_id=agent_id,
        on_event=on_event or _default_event,
        on_tick=on_tick or _default_tick,
    )


def _spy_timer_delays(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record every delay the TIMER machinery arms, and nothing else.

    Must be called from inside the running loop.  The spy sits on the
    shared event loop, so asyncio's own scheduling lands here too — and
    the filter is load-bearing rather than tidy: on Python 3.11
    ``asyncio.wait_for`` schedules its timeout through ``call_later``,
    so an unfiltered spy records the ``wait_for(timeout=2.0)`` below
    *between* the initial arm and the re-arm and shifts every positional
    assertion by one.  Python 3.12 routes ``wait_for`` through
    ``asyncio.timeout`` → ``call_at`` instead, which is why an
    unfiltered spy passes on 3.12 and fails on 3.11 — the version this
    project targets and CI runs.

    ``_fire`` is the callback both the first-arm and re-arm sites in
    ``event_loop_timers.py`` pass, so filtering on it captures exactly
    the production call sites these tests mean to observe.
    """
    delays: list[float] = []
    running = asyncio.get_running_loop()
    original = running.call_later

    def _spy(delay, callback, *args, context=None):  # type: ignore[no-untyped-def]
        if getattr(callback, "__name__", "") == "_fire":
            delays.append(float(delay))
        return original(delay, callback, *args, context=context)

    monkeypatch.setattr(running, "call_later", _spy)
    return delays


class TestInitialDelay:
    async def test_initial_delay_overrides_first_fire(self, monkeypatch):
        """The first ``ScheduledWake`` fires at ``initial_delay`` regardless
        of ``interval``; subsequent re-arms use ``interval``.

        Why: the restart-mid-jitter-window path needs the persona to
        resume at the saved monotonic anchor, not at a fresh
        ``interval``-shaped delay.  Without the override the persona
        would fire ``interval`` seconds after restart, even if the
        previous run was 1ms away from its next fire when the process
        died — a noticeable cadence skew for long-interval reflection
        timers.
        """
        monkeypatch.setattr(EventLoop, "_MIN_INTERVAL", 0.001)

        fires = 0
        seen = asyncio.Event()

        async def _on_tick(wake: ScheduledWake) -> None:
            nonlocal fires
            fires += 1
            if fires >= 2:
                seen.set()

        loop = _build_loop(on_tick=_on_tick)
        loop.start()
        try:
            # Capture the delays the timer arms; first is the initial-delay
            # arm, subsequent ones are re-arms.
            delays = _spy_timer_delays(monkeypatch)
            loop.register_timer(
                timer_id="restored",
                callback_kind="memory_consolidation",
                interval=0.05,
                initial_delay=0.005,
            )
            await asyncio.wait_for(seen.wait(), timeout=2.0)
        finally:
            await loop.stop(timeout=1.0)

        assert delays, "expected at least one call_later delay captured"
        # First arm uses the restored anchor …
        assert delays[0] == pytest.approx(0.005, abs=1e-6)
        # … re-arm uses interval (no jitter configured).
        assert delays[1] == pytest.approx(0.05, abs=1e-6)

    async def test_initial_delay_none_preserves_legacy_first_fire(
        self, monkeypatch,
    ):
        """Omitting ``initial_delay`` reproduces the PR 2 first-fire shape.

        Back-compat for every existing caller — the PR 2 wiring
        (legacy_tick adapter, autonomy.timers loader pre-2.1) does not
        pass ``initial_delay`` and must keep its existing cadence.
        """
        monkeypatch.setattr(EventLoop, "_MIN_INTERVAL", 0.001)

        async def _on_tick(wake: ScheduledWake) -> None:
            pass

        loop = _build_loop(on_tick=_on_tick)
        loop.start()
        try:
            delays = _spy_timer_delays(monkeypatch)
            loop.register_timer(
                timer_id="fresh",
                callback_kind="any",
                interval=0.05,
                # initial_delay omitted — default first-fire delay
            )
            await asyncio.sleep(0.01)
        finally:
            await loop.stop(timeout=1.0)

        assert delays, "expected at least one call_later delay captured"
        # No initial_delay → first arm uses interval (no jitter).
        assert delays[0] == pytest.approx(0.05, abs=1e-6)

    async def test_initial_delay_rejects_negative(self):
        """A negative ``initial_delay`` is a clamping-bug signal — reject
        at the API boundary so a buggy caller cannot enqueue a
        fire-immediately wake disguised as a scheduled re-arm."""
        loop = _build_loop()
        with pytest.raises(ValueError, match="initial_delay"):
            loop.register_timer(
                timer_id="neg",
                callback_kind="any",
                interval=10.0,
                initial_delay=-0.1,
            )

    async def test_initial_delay_zero_accepted(self):
        """``initial_delay=0.0`` is the boundary — accepted; means
        "fire on next event-loop iteration", which is the contract the
        clamp-to-[_MIN_INTERVAL, interval+jitter] caller may produce when
        the saved anchor has already elapsed."""
        loop = _build_loop()
        loop.start()
        try:
            loop.register_timer(
                timer_id="zero-delay",
                callback_kind="any",
                interval=10.0,
                initial_delay=0.0,
            )
            assert loop.has_timer("zero-delay")
        finally:
            await loop.stop(timeout=1.0)

    async def test_initial_delay_rejected_for_one_shot(self):
        """One-shot timers already carry their fire delay in
        ``fire_after``; passing both is ambiguous and rejected."""
        loop = _build_loop()
        with pytest.raises(ValueError, match="initial_delay"):
            loop.register_timer(
                timer_id="one-shot-with-initial",
                callback_kind="any",
                fire_after=0.05,
                initial_delay=0.01,
            )
