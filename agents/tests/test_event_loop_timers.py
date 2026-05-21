"""Unit tests for ``EventLoop.register_timer`` enhancements — RFC 0024 PR 2.

Phase 2 layers three capabilities on top of PR 1's basic periodic timer:

* **Jitter** — ``jitter_max`` randomises each re-arm by up to
  ``±jitter_max`` seconds.  Used by RFC 0024 §C to spread fan-out of
  identically-configured personas.
* **One-shot** — ``next_fire_at`` registers a non-periodic timer that
  fires exactly once at the monotonic-clock anchor.  Reserved for
  Phase 3+ (e.g. salience-wake reminder); no schema surface in Phase 2.
* **Busy-loop guard** — registering a timer with ``interval`` below the
  ``_MIN_INTERVAL`` floor (1.0s) raises ``ValueError`` at the API
  boundary so a programmatic caller cannot bypass the schema's
  ``minimum: 1.0`` constraint.

The original PR 1 ``register_timer`` periodic-firing test in
``test_event_loop.py`` still passes — these tests pin the *new* surface.
"""

from __future__ import annotations

import asyncio
import random
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


class TestBusyLoopGuard:
    """Per RFC 0024 §Security Considerations — reject ``interval < _MIN_INTERVAL``
    at the ``register_timer`` API boundary so a programmatic caller can't
    bypass the schema's ``minimum: 1.0`` constraint."""

    async def test_rejects_interval_below_min(self):
        loop = _build_loop()
        with pytest.raises(ValueError, match="interval"):
            loop.register_timer(
                timer_id="too-fast",
                callback_kind="memory_consolidation",
                interval=0.001,
            )

    async def test_rejects_zero_interval(self):
        loop = _build_loop()
        with pytest.raises(ValueError, match="interval"):
            loop.register_timer(
                timer_id="zero",
                callback_kind="any",
                interval=0.0,
            )

    async def test_rejects_negative_interval(self):
        loop = _build_loop()
        with pytest.raises(ValueError, match="interval"):
            loop.register_timer(
                timer_id="neg",
                callback_kind="any",
                interval=-5.0,
            )

    async def test_accepts_exact_min(self):
        """``interval == _MIN_INTERVAL`` (1.0s) is the boundary — accepted."""
        loop = _build_loop()
        loop.start()
        try:
            loop.register_timer(
                timer_id="boundary",
                callback_kind="any",
                interval=1.0,
            )
            assert loop.has_timer("boundary")
        finally:
            await loop.stop(timeout=1.0)

    async def test_rejects_negative_jitter_max(self):
        """``jitter_max < 0`` is rejected at the API boundary.

        Defense-in-depth parity with the schema's ``minimum: 0.0`` on
        ``jitter_max_seconds`` — without this check a negative value
        silently passes through to ``random.uniform`` (which swaps
        endpoints), shrinking the effective interval below ``_MIN_INTERVAL``.
        """
        loop = _build_loop()
        with pytest.raises(ValueError, match="jitter_max"):
            loop.register_timer(
                timer_id="neg-jitter",
                callback_kind="any",
                interval=10.0,
                jitter_max=-0.1,
            )

    async def test_rejects_jitter_max_exceeding_interval_floor_slack(self):
        """``jitter_max`` must leave at least ``_MIN_INTERVAL`` of slack
        below ``interval`` — otherwise ``random.uniform(-jitter, +jitter)``
        can draw a re-arm delay below the busy-loop floor (or even
        negative, which asyncio treats as fire-on-next-iteration).

        Boundary: ``jitter_max == interval - _MIN_INTERVAL`` is the
        largest legal value; anything strictly larger is rejected.
        """
        loop = _build_loop()
        # _MIN_INTERVAL defaults to 1.0; interval=2.0 → max legal jitter 1.0.
        with pytest.raises(ValueError, match="jitter_max"):
            loop.register_timer(
                timer_id="busy-jitter",
                callback_kind="any",
                interval=2.0,
                jitter_max=1.5,
            )

    async def test_rejects_jitter_max_at_minimum_interval(self):
        """At ``interval == _MIN_INTERVAL`` the only legal ``jitter_max`` is
        ``0`` — any positive jitter would push some draws below the floor.

        Corollary of the cap above; pins the corner case explicitly so a
        future relaxation of the cap surfaces here first.
        """
        loop = _build_loop()
        with pytest.raises(ValueError, match="jitter_max"):
            loop.register_timer(
                timer_id="floor-jitter",
                callback_kind="any",
                interval=1.0,
                jitter_max=0.5,
            )

    async def test_accepts_jitter_max_at_floor_slack_boundary(self):
        """``jitter_max == interval - _MIN_INTERVAL`` is the exact
        boundary — accepted because the lowest possible draw lands on
        ``_MIN_INTERVAL`` itself, which the floor admits."""
        loop = _build_loop()
        loop.start()
        try:
            loop.register_timer(
                timer_id="boundary-jitter",
                callback_kind="any",
                interval=2.0,
                jitter_max=1.0,  # 2.0 - 1.0 == _MIN_INTERVAL
            )
            assert loop.has_timer("boundary-jitter")
        finally:
            await loop.stop(timeout=1.0)


class TestJitter:
    """``jitter_max`` randomises each re-arm by up to ``±jitter_max`` seconds.

    The jitter draw stays in ``[interval - jitter_max, interval + jitter_max]``
    and the timer's first-fire delay also draws from that range.  Tests use
    a seeded RNG via ``monkeypatch`` so the assertion is deterministic
    without resorting to a statistical bound.
    """

    async def test_jitter_calls_random_uniform_with_correct_bounds(
        self, monkeypatch,
    ):
        """Every re-arm draws from ``random.uniform(-jitter_max, +jitter_max)``.

        Patches the module-level ``random.uniform`` so the test pins the
        bounds passed to it without depending on call_later timing.
        """
        monkeypatch.setattr(EventLoop, "_MIN_INTERVAL", 0.01)

        uniform_args: list[tuple[float, float]] = []

        def _spy(lo: float, hi: float) -> float:
            uniform_args.append((lo, hi))
            return 0.0  # deterministic — no actual jitter applied

        monkeypatch.setattr("agents.event_loop.random.uniform", _spy)

        seen = asyncio.Event()
        fires = 0

        async def _on_tick(wake: ScheduledWake) -> None:
            nonlocal fires
            fires += 1
            if fires >= 3:
                seen.set()

        loop = _build_loop(on_tick=_on_tick)
        loop.start()
        try:
            loop.register_timer(
                timer_id="jitter-test",
                callback_kind="memory_consolidation",
                interval=0.05,
                jitter_max=0.02,
            )
            await asyncio.wait_for(seen.wait(), timeout=2.0)
        finally:
            await loop.stop(timeout=1.0)

        # Every draw passes ``(-jitter_max, +jitter_max)`` bounds.
        assert uniform_args, "expected random.uniform calls for the jitter draws"
        for lo, hi in uniform_args:
            assert lo == -0.02
            assert hi == 0.02

    async def test_jitter_zero_default(self, monkeypatch):
        """Omitting ``jitter_max`` (or passing ``0``) reproduces PR 1's exact
        cadence — ``random.uniform`` is never called for a no-jitter timer."""
        uniform_calls = 0

        real_uniform = random.uniform

        def _spy(lo, hi):
            nonlocal uniform_calls
            uniform_calls += 1
            return real_uniform(lo, hi)

        monkeypatch.setattr("agents.event_loop.random.uniform", _spy)

        async def _on_tick(wake: ScheduledWake) -> None:
            pass

        loop = _build_loop(on_tick=_on_tick)
        loop.start()
        try:
            loop.register_timer(
                timer_id="no-jitter",
                callback_kind="any",
                interval=1.0,
                # jitter_max omitted — defaults to 0.0
            )
            await asyncio.sleep(0.05)
        finally:
            await loop.stop(timeout=1.0)

        assert uniform_calls == 0


class TestOneShot:
    """One-shot timers fire exactly once at ``next_fire_at`` and do not re-arm.

    Phase 2 ships the dataclass surface internally so RFC 0024 Phase 3+ can
    schedule single-shot reminders without a Phase 2 schema follow-up. The
    ``agents.yaml`` schema does NOT expose this — only ``interval_seconds``.
    """

    async def test_one_shot_fires_exactly_once(self):
        ticks: list[ScheduledWake] = []
        seen = asyncio.Event()

        async def _on_tick(wake: ScheduledWake) -> None:
            ticks.append(wake)
            seen.set()

        loop = _build_loop(on_tick=_on_tick)
        loop.start()
        try:
            loop.register_timer(
                timer_id="one-shot",
                callback_kind="reminder",
                interval=None,  # one-shot
                fire_after=0.05,
            )
            await asyncio.wait_for(seen.wait(), timeout=2.0)
            # Wait long enough that a periodic re-arm would have fired again.
            await asyncio.sleep(0.2)
        finally:
            await loop.stop(timeout=1.0)

        assert len(ticks) == 1
        assert ticks[0].timer_id == "one-shot"
        assert ticks[0].callback_kind == "reminder"
        # One-shot timers are auto-cleaned from the registry after they fire
        # so a subsequent `register_timer` with the same id is not a conflict.
        assert not loop.has_timer("one-shot")

    async def test_one_shot_rejects_invalid_combo(self):
        """Exactly one of ``interval`` (periodic) or ``fire_after``
        (one-shot) must be set; both/neither raises ``ValueError``."""
        loop = _build_loop()
        with pytest.raises(ValueError):
            loop.register_timer(
                timer_id="both",
                callback_kind="any",
                interval=1.0,
                fire_after=2.0,
            )
        with pytest.raises(ValueError):
            loop.register_timer(
                timer_id="neither",
                callback_kind="any",
                interval=None,
            )
