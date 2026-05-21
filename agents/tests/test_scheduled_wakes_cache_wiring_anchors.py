"""Restart-mid-jitter-window anchor tests for ``ScheduledWakesCache``
↔ ``initialize_persona_agents`` — RFC 0024 PR 2.1.

Companion to ``test_scheduled_wakes_cache_wiring.py`` (lifecycle /
orphan / interval rounding) — split to stay under the 500-line
review-friendly cap.  Tests here pin the load-bearing contract that
distinguishes PR 2.1 from PR 2: a persona restarted while a saved
``next_fire_at_ms`` lies in the future must arm the timer at the
saved anchor rather than firing immediately or after a fresh
``interval``.

Scope decision recorded here: **``next_fire_at_ms`` clock** —
monotonic (``time.monotonic_ns() // 1_000_000``) per RFC 0024 §C;
restoration converts back the same way.  Recorded so a future
"wall-clock drift" report has a name to grep for.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from agents.dispatch import EventDispatcher
from agents.memory.scheduled_wakes import ScheduledWakeRow, ScheduledWakesCache
from agents.persona import create_persona_agent
from agents.server_persona import initialize_persona_agents
from agents.tests._scheduled_wakes_wiring_helpers import (
    make_client,
    persona_config,
)
from agents.tools.registry import clear_registry


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


class TestMidJitterRestart:
    """Verifies the load-bearing PR 2.1 contract by spying on
    :meth:`EventLoop.register_timer` to capture the ``initial_delay``
    the loader computed.
    """

    async def test_future_anchor_passed_as_initial_delay(
        self, tmp_path: Path, monkeypatch,
    ):
        db_path = str(tmp_path / "memory.db")

        # Seed the cache with a row whose anchor sits ~10s in the future.
        now_ms = time.monotonic_ns() // 1_000_000
        saved_anchor_ms = now_ms + 10_000
        seed = ScheduledWakesCache(db_path=db_path, agent_id="ember-owl")
        await seed.initialize()
        await seed.rebuild_from_config([
            ScheduledWakeRow(
                timer_id="memory_consolidation",
                kind="memory_consolidation",
                interval_ms=30_000,
                jitter_ms=0,
                next_fire_at_ms=saved_anchor_ms,
            ),
        ])
        await seed.close()

        config = persona_config(
            db_path=db_path,
            timers=[{
                "id": "memory_consolidation",
                "interval_seconds": 30,
                "kind": "memory_consolidation",
            }],
        )
        agent = create_persona_agent(
            agent_id="ember-owl", config=config, llm_client=make_client(),
        )
        dispatcher = EventDispatcher()
        schedulers: dict = {}
        caches: dict[str, ScheduledWakesCache] = {}

        # Spy on register_timer to capture initial_delay.
        from agents.event_loop import EventLoop
        captured_kwargs: list[dict[str, Any]] = []
        real_register = EventLoop.register_timer

        def _spy(self, **kwargs):  # type: ignore[no-untyped-def]
            captured_kwargs.append(dict(kwargs))
            return real_register(self, **kwargs)

        monkeypatch.setattr(EventLoop, "register_timer", _spy)

        await initialize_persona_agents(
            {"ember-owl": agent}, dispatcher, schedulers,
            scheduled_wakes_caches=caches,
        )

        restored = [
            kw for kw in captured_kwargs
            if kw.get("timer_id") == "memory_consolidation"
        ]
        assert restored, (
            f"expected register_timer to be called for memory_consolidation; "
            f"captured: {captured_kwargs}"
        )
        initial_delay = restored[0].get("initial_delay")
        assert initial_delay is not None, (
            "expected initial_delay to be set when a future anchor is restored"
        )
        # The seed-anchor was now+10s; elapsed since seed is <1s for a
        # CI worker, so the captured delay sits in [9s, 10s].
        assert 9.0 <= initial_delay <= 10.0, (
            f"initial_delay {initial_delay}s should approximate saved 10s anchor"
        )

        await schedulers["ember-owl"].stop()
        await caches["ember-owl"].close()
        await agent.close_memory()

    async def test_past_anchor_does_not_pass_initial_delay(
        self, tmp_path: Path, monkeypatch,
    ):
        """A saved anchor in the past means the timer was overdue when
        the persona died.  PR 2.1's contract: arm with the default
        first-fire shape, not ``initial_delay = negative_seconds``.

        Without this, the loader would emit a negative ``initial_delay``
        which the API boundary now rejects — so the wiring code is
        responsible for the "if past, no override" branch.
        """
        db_path = str(tmp_path / "memory.db")

        now_ms = time.monotonic_ns() // 1_000_000
        past_anchor_ms = max(0, now_ms - 5_000)
        seed = ScheduledWakesCache(db_path=db_path, agent_id="ember-owl")
        await seed.initialize()
        await seed.rebuild_from_config([
            ScheduledWakeRow(
                timer_id="memory_consolidation",
                kind="memory_consolidation",
                interval_ms=30_000,
                jitter_ms=0,
                next_fire_at_ms=past_anchor_ms,
            ),
        ])
        await seed.close()

        config = persona_config(
            db_path=db_path,
            timers=[{
                "id": "memory_consolidation",
                "interval_seconds": 30,
                "kind": "memory_consolidation",
            }],
        )
        agent = create_persona_agent(
            agent_id="ember-owl", config=config, llm_client=make_client(),
        )
        dispatcher = EventDispatcher()
        schedulers: dict = {}
        caches: dict[str, ScheduledWakesCache] = {}

        from agents.event_loop import EventLoop
        captured_kwargs: list[dict[str, Any]] = []
        real_register = EventLoop.register_timer

        def _spy(self, **kwargs):  # type: ignore[no-untyped-def]
            captured_kwargs.append(dict(kwargs))
            return real_register(self, **kwargs)

        monkeypatch.setattr(EventLoop, "register_timer", _spy)

        await initialize_persona_agents(
            {"ember-owl": agent}, dispatcher, schedulers,
            scheduled_wakes_caches=caches,
        )

        restored = [
            kw for kw in captured_kwargs
            if kw.get("timer_id") == "memory_consolidation"
        ]
        assert restored
        # Past anchor → loader does NOT override.
        assert restored[0].get("initial_delay") is None

        await schedulers["ember-owl"].stop()
        await caches["ember-owl"].close()
        await agent.close_memory()

    async def test_fresh_timer_gets_no_initial_delay(
        self, tmp_path: Path, monkeypatch,
    ):
        """A timer that has no saved row (first time configured) arms
        with the default first-fire shape — no ``initial_delay`` kwarg."""
        db_path = str(tmp_path / "memory.db")
        config = persona_config(
            db_path=db_path,
            timers=[{
                "id": "brand_new",
                "interval_seconds": 30,
                "kind": "any",
            }],
        )
        agent = create_persona_agent(
            agent_id="ember-owl", config=config, llm_client=make_client(),
        )
        dispatcher = EventDispatcher()
        schedulers: dict = {}
        caches: dict[str, ScheduledWakesCache] = {}

        from agents.event_loop import EventLoop
        captured_kwargs: list[dict[str, Any]] = []
        real_register = EventLoop.register_timer

        def _spy(self, **kwargs):  # type: ignore[no-untyped-def]
            captured_kwargs.append(dict(kwargs))
            return real_register(self, **kwargs)

        monkeypatch.setattr(EventLoop, "register_timer", _spy)

        await initialize_persona_agents(
            {"ember-owl": agent}, dispatcher, schedulers,
            scheduled_wakes_caches=caches,
        )

        restored = [
            kw for kw in captured_kwargs if kw.get("timer_id") == "brand_new"
        ]
        assert restored
        assert restored[0].get("initial_delay") is None

        await schedulers["ember-owl"].stop()
        await caches["ember-owl"].close()
        await agent.close_memory()


class TestNextFireAtPersistence:
    """After init, the cache's ``next_fire_at_ms`` reflects the planned
    monotonic anchor of the *next* fire so a subsequent restart can
    honour it.  Without this, every restart loses the anchor and the
    mid-jitter-restart guarantee silently degrades."""

    async def test_next_fire_at_ms_written_in_monotonic_clock(
        self, tmp_path: Path,
    ):
        db_path = str(tmp_path / "memory.db")
        config = persona_config(
            db_path=db_path,
            timers=[{
                "id": "memory_consolidation",
                "interval_seconds": 30,
                "kind": "memory_consolidation",
            }],
        )
        agent = create_persona_agent(
            agent_id="ember-owl", config=config, llm_client=make_client(),
        )
        dispatcher = EventDispatcher()
        schedulers: dict = {}
        caches: dict[str, ScheduledWakesCache] = {}

        before_ms = time.monotonic_ns() // 1_000_000
        await initialize_persona_agents(
            {"ember-owl": agent}, dispatcher, schedulers,
            scheduled_wakes_caches=caches,
        )
        after_ms = time.monotonic_ns() // 1_000_000

        row = next(
            r for r in await caches["ember-owl"].list_timers()
            if r.timer_id == "memory_consolidation"
        )
        # Planned next-fire anchor sits ~30s after init in monotonic
        # milliseconds.  Bounds tolerate up to 100ms of init overhead.
        assert before_ms + 30_000 <= row.next_fire_at_ms <= after_ms + 30_000 + 100

        await schedulers["ember-owl"].stop()
        await caches["ember-owl"].close()
        await agent.close_memory()


class TestStaleAnchorClamp:
    """Stale-anchor robustness — RFC 0024 PR 2.1 review follow-up.

    ``next_fire_at_ms`` is written in :func:`time.monotonic_ns` (RFC 0024
    §C, "avoid wall-clock drift over long uptime").  ``time.monotonic()``
    is process-stable by Python contract; cross-process stability is a
    *platform* property — Linux/macOS/Windows currently expose system- or
    session-uptime so the value survives an agent-process restart, but
    that property silently breaks across a system reboot (the new boot
    starts from a fresh monotonic epoch).

    The loader's ``min(raw, interval + jitter_max)`` clamp in
    :func:`agents.server_persona_timers._register_configured_timers`
    means a meaningless saved anchor (post-reboot, corrupted, or
    written by a future RFC 0024 ``RegisterTimer`` RPC with a bug)
    is *bounded* to the same upper envelope as a fresh first-fire —
    the persona fires within ``interval + jitter_max`` of restart
    rather than after some arbitrary delay.  This test pins that
    contract so a future refactor that drops the upper clamp
    (e.g. "just pass raw through, trust the caller") surfaces the
    silent-degradation risk in CI.
    """

    async def test_far_future_anchor_clamps_to_interval_plus_jitter(
        self, tmp_path: Path, monkeypatch,
    ):
        """Saved anchor wildly in the future (simulates the cross-
        process-monotonic break after a system reboot) → ``initial_delay``
        captured is ``interval + jitter_max``, not the raw seconds-until
        value.  The persona fires within bounded time even when the
        anchor is meaningless."""
        db_path = str(tmp_path / "memory.db")

        # Seed an anchor 1000 seconds in the future against a 30s
        # interval / 5s jitter — far past the upper clamp of 35s.  In
        # production this corresponds to "previous process recorded an
        # anchor against a higher monotonic epoch than the new process
        # observes" (one of: system reboot, monotonic-clock platform
        # quirk, manually-corrupted row, future-RPC bug).
        now_ms = time.monotonic_ns() // 1_000_000
        stale_anchor_ms = now_ms + 1_000_000  # 1000 seconds
        seed = ScheduledWakesCache(db_path=db_path, agent_id="ember-owl")
        await seed.initialize()
        await seed.rebuild_from_config([
            ScheduledWakeRow(
                timer_id="memory_consolidation",
                kind="memory_consolidation",
                interval_ms=30_000,
                jitter_ms=5_000,
                next_fire_at_ms=stale_anchor_ms,
            ),
        ])
        await seed.close()

        config = persona_config(
            db_path=db_path,
            timers=[{
                "id": "memory_consolidation",
                "interval_seconds": 30,
                "jitter_max_seconds": 5,
                "kind": "memory_consolidation",
            }],
        )
        agent = create_persona_agent(
            agent_id="ember-owl", config=config, llm_client=make_client(),
        )
        dispatcher = EventDispatcher()
        schedulers: dict = {}
        caches: dict[str, ScheduledWakesCache] = {}

        from agents.event_loop import EventLoop
        captured_kwargs: list[dict[str, Any]] = []
        real_register = EventLoop.register_timer

        def _spy(self, **kwargs):  # type: ignore[no-untyped-def]
            captured_kwargs.append(dict(kwargs))
            return real_register(self, **kwargs)

        monkeypatch.setattr(EventLoop, "register_timer", _spy)

        await initialize_persona_agents(
            {"ember-owl": agent}, dispatcher, schedulers,
            scheduled_wakes_caches=caches,
        )

        restored = [
            kw for kw in captured_kwargs
            if kw.get("timer_id") == "memory_consolidation"
        ]
        assert restored, (
            f"expected register_timer call for memory_consolidation; "
            f"captured: {captured_kwargs}"
        )
        initial_delay = restored[0].get("initial_delay")
        # raw = 1000s; upper = interval + jitter_max = 35s; clamp picks
        # upper exactly.  The loader does not currently emit any log on
        # the clamp branch, but the captured value is the hard contract
        # the API exposes — pin it.
        assert initial_delay == pytest.approx(35.0, abs=1e-6), (
            f"stale anchor should clamp to interval+jitter_max=35s; "
            f"got {initial_delay}s"
        )

        await schedulers["ember-owl"].stop()
        await caches["ember-owl"].close()
        await agent.close_memory()
