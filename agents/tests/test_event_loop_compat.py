"""RFC 0024 Phase 1 config-compat regression.

Pins the v0.3.2 ``tick_interval_seconds`` cadence under the new
:class:`agents.event_loop.EventLoop` substrate so an unmodified
``agents.yaml`` produces the same observable wake cadence as the future
Phase-2 ``timers: [{id: "legacy_tick", interval_seconds: 60}]`` equivalent.

The cadence assertion is loose by design — wall-clock asyncio fires under
test machines vary, so the contract is *one ScheduledWake per interval, no
more, no fewer over N intervals*, not "fires at exactly t=N*interval".
"""

from __future__ import annotations

import asyncio
from typing import Any

from agents.event_loop import EventLoop, ScheduledWake


class TestLegacyTickCadence:
    async def test_one_wake_per_interval_over_window(self):
        """A single legacy timer at ``interval`` produces exactly N wakes
        over ``N * interval`` seconds (±1 for race against stop)."""
        ticks: list[ScheduledWake] = []

        async def _on_tick(wake: ScheduledWake) -> None:
            ticks.append(wake)

        async def _on_event(event: Any) -> list[Any]:
            return []

        loop = EventLoop(
            agent_id="compat-agent",
            on_event=_on_event,
            on_tick=_on_tick,
        )
        loop.start()
        try:
            loop.register_timer(
                timer_id="legacy_tick",
                callback_kind="tick",
                interval=0.05,
            )
            # 5 intervals → expect ~5 wakes.
            await asyncio.sleep(0.27)
        finally:
            await loop.stop(timeout=1.0)

        # Allow ±1 for race against stop / scheduling slack.  The
        # invariant is "no busy-fire, no skipped intervals".
        assert 4 <= len(ticks) <= 6, f"expected ~5 fires, got {len(ticks)}"
        for wake in ticks:
            assert wake.timer_id == "legacy_tick"
            assert wake.callback_kind == "tick"

    async def test_legacy_timer_carries_legacy_id(self):
        """The legacy back-compat path uses ``timer_id='legacy_tick'`` per
        RFC 0024 §B — Phase 2 ``autonomy.timers`` will use the operator's
        chosen ids; Phase 1's synthesised back-compat path is the only one
        named ``legacy_tick``."""
        ticks: list[ScheduledWake] = []

        async def _on_tick(wake: ScheduledWake) -> None:
            ticks.append(wake)

        async def _on_event(event: Any) -> list[Any]:
            return []

        loop = EventLoop(
            agent_id="compat-agent",
            on_event=_on_event,
            on_tick=_on_tick,
        )
        loop.start()
        try:
            loop.register_timer(
                timer_id="legacy_tick",
                callback_kind="tick",
                interval=0.05,
            )
            for _ in range(200):
                if ticks:
                    break
                await asyncio.sleep(0.01)
        finally:
            await loop.stop(timeout=1.0)

        assert ticks, "legacy timer must fire at least once"
        assert ticks[0].timer_id == "legacy_tick"

    async def test_tick_scheduler_synthesises_legacy_timer(self):
        """The :class:`agents.tick.TickScheduler` adapter must register
        exactly one timer (``legacy_tick``) on ``start()`` so the dispatch
        path's ``scheduler.event_loop`` exposes a single periodic producer
        — the v0.3.2 cadence under the new substrate."""
        # Lazy import to keep this test focused on the substrate; using
        # the adapter requires a real ``_LLMPersonaAgent``, which is heavy.
        # Instead, verify the contract through the EventLoop public API.
        from agents.tick import TickScheduler

        class _MinimalAgent:
            agent_id = "compat-agent"

            def exclusive(self) -> Any:  # pragma: no cover — not called here
                raise NotImplementedError

            def recover_idle_energy(self) -> None:  # pragma: no cover
                raise NotImplementedError

            async def on_tick(self) -> list[Any]:  # pragma: no cover
                return []

            async def on_event(self, event: Any) -> list[Any]:  # pragma: no cover
                return []

        TickScheduler._MIN_INTERVAL = 0.01  # type: ignore[assignment]
        try:
            scheduler = TickScheduler(
                _MinimalAgent(),  # type: ignore[arg-type]
                interval=0.05,
            )
            scheduler.start()
            try:
                # One timer, with the legacy id, exposed on the underlying loop.
                assert "legacy_tick" in scheduler.event_loop._timers
                assert len(scheduler.event_loop._timers) == 1
            finally:
                await scheduler.stop(timeout=1.0)
        finally:
            TickScheduler._MIN_INTERVAL = 1.0  # type: ignore[assignment]
