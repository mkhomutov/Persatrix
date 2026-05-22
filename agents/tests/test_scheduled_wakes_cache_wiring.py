"""Wiring tests for ``ScheduledWakesCache`` ↔ ``initialize_persona_agents``
— RFC 0024 PR 2.1.  Lifecycle / orphan / interval-rounding contracts.

PR 2 shipped the ``scheduled_wakes`` table schema and the
:class:`ScheduledWakesCache` class but did **not** wire either into the
persona-agents init path — the restart-mid-jitter-window guarantee the
:mod:`agents.memory.scheduled_wakes` module docstring names is therefore
not yet delivered.  PR 2.1 closes the gap.

Tests pinning the restart-mid-jitter-window contract (saved
``next_fire_at_ms`` → ``initial_delay``) live in the sibling file
``test_scheduled_wakes_cache_wiring_anchors.py`` — split to stay under
the 500-line review-friendly cap.

Scope decision recorded here: **``interval_ms`` rounding** —
``round(seconds * 1000)`` per the PR 2.1 plan.  Integer ``interval_seconds``
maps exactly (``30 → 30000``); fractional values round to nearest ms
(``1.501 → 1501``).  ``int(seconds * 1000)`` was the alternative — rejected
because it truncates ``1.5001 → 1500`` which silently widens the cadence
at every config touch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agents.dispatch import EventDispatcher
from agents.memory.scheduled_wakes import ScheduledWakeRow, ScheduledWakesCache
from agents.persona import create_persona_agent
from agents.server_persona import initialize_persona_agents
from agents.server_persona_timers import _build_scheduled_wake_rows
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


class TestCacheLifecycle:
    """``initialize_persona_agents`` opens, rebuilds, and stores one cache
    per persona that declares ``autonomy.timers``."""

    async def test_cache_created_when_timers_configured(self, tmp_path: Path):
        """A persona with ``autonomy.timers`` gets a cache opened against
        its ``memory.db_path``; the cache lives in the caller's dict so
        the server's existing shutdown path can close it."""
        db_path = str(tmp_path / "memory.db")
        timers = [
            {
                "id": "memory_consolidation",
                "interval_seconds": 30,
                "kind": "memory_consolidation",
            },
        ]
        config = persona_config(db_path=db_path, timers=timers)
        agent = create_persona_agent(
            agent_id="ember-owl", config=config, llm_client=make_client(),
        )
        dispatcher = EventDispatcher()
        schedulers: dict = {}
        caches: dict[str, ScheduledWakesCache] = {}

        await initialize_persona_agents(
            {"ember-owl": agent}, dispatcher, schedulers,
            scheduled_wakes_caches=caches,
        )

        assert "ember-owl" in caches
        rows = await caches["ember-owl"].list_timers()
        assert {r.timer_id for r in rows} == {"memory_consolidation"}

        await schedulers["ember-owl"].stop()
        await caches["ember-owl"].close()
        await agent.close_memory()

    async def test_no_cache_when_legacy_tick_only(self, tmp_path: Path):
        """The ``tick_interval_seconds``-only path predates the cache —
        no cache opened, no row written.  Preserves the back-compat
        contract that PR 2.1's wiring is opt-in via ``autonomy.timers``.
        """
        db_path = str(tmp_path / "memory.db")
        config = persona_config(db_path=db_path, timers=None)
        config["autonomy"]["tick_interval_seconds"] = 60

        agent = create_persona_agent(
            agent_id="ember-owl", config=config, llm_client=make_client(),
        )
        dispatcher = EventDispatcher()
        schedulers: dict = {}
        caches: dict[str, ScheduledWakesCache] = {}

        await initialize_persona_agents(
            {"ember-owl": agent}, dispatcher, schedulers,
            scheduled_wakes_caches=caches,
        )

        assert "ember-owl" not in caches

        await schedulers["ember-owl"].stop()
        await agent.close_memory()

    async def test_cache_param_optional(self, tmp_path: Path):
        """``scheduled_wakes_caches`` is keyword-only and defaults to
        ``None`` — existing call sites and tests must keep working
        without passing the new dict."""
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

        # No scheduled_wakes_caches arg — must not raise.
        await initialize_persona_agents(
            {"ember-owl": agent}, dispatcher, schedulers,
        )

        assert schedulers["ember-owl"].event_loop.has_timer("memory_consolidation")

        await schedulers["ember-owl"].stop()
        await agent.close_memory()


class TestOrphanCleanup:
    """A timer removed from ``agents.yaml`` between restarts must be
    deleted from the cache on the next bring-up — the cache cannot
    drift from config."""

    async def test_orphan_row_deleted_on_init(self, tmp_path: Path):
        db_path = str(tmp_path / "memory.db")

        # Pre-populate the cache with two timers as if from a previous run.
        seed = ScheduledWakesCache(db_path=db_path, agent_id="ember-owl")
        await seed.initialize()
        await seed.rebuild_from_config([
            ScheduledWakeRow(
                timer_id="memory_consolidation",
                kind="memory_consolidation",
                interval_ms=30_000,
                jitter_ms=0,
                next_fire_at_ms=0,
            ),
            ScheduledWakeRow(
                timer_id="removed_in_yaml",
                kind="any",
                interval_ms=60_000,
                jitter_ms=0,
                next_fire_at_ms=0,
            ),
        ])
        await seed.close()

        # New config drops ``removed_in_yaml``.
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

        await initialize_persona_agents(
            {"ember-owl": agent}, dispatcher, schedulers,
            scheduled_wakes_caches=caches,
        )

        rows = await caches["ember-owl"].list_timers()
        assert {r.timer_id for r in rows} == {"memory_consolidation"}

        await schedulers["ember-owl"].stop()
        await caches["ember-owl"].close()
        await agent.close_memory()


class TestIntervalMsRounding:
    """Scope decision: ``interval_seconds: 1.5`` → ``interval_ms = 1500``
    via ``round(...)``.  Recorded here so a future refactor that flips
    to ``int(seconds * 1000)`` surfaces in CI."""

    async def test_integer_seconds_maps_to_exact_ms(self, tmp_path: Path):
        db_path = str(tmp_path / "memory.db")
        config = persona_config(
            db_path=db_path,
            timers=[{
                "id": "thirty_sec",
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

        await initialize_persona_agents(
            {"ember-owl": agent}, dispatcher, schedulers,
            scheduled_wakes_caches=caches,
        )

        row = next(
            r for r in await caches["ember-owl"].list_timers()
            if r.timer_id == "thirty_sec"
        )
        assert row.interval_ms == 30_000

        await schedulers["ember-owl"].stop()
        await caches["ember-owl"].close()
        await agent.close_memory()

    async def test_fractional_seconds_rounds_to_nearest_ms(self, tmp_path: Path):
        """``1.501s → 1501ms`` via ``round``.  Picks ``1.501`` (not
        ``1.5005``) so the float-representation truncation does not
        flip the rounded value — the rule is "round to nearest ms";
        the test asserts that contract on an unambiguous fractional
        value.  The exact rule lives in ``server_persona.py``."""
        db_path = str(tmp_path / "memory.db")
        config = persona_config(
            db_path=db_path,
            timers=[{
                "id": "frac",
                "interval_seconds": 1.501,
                "kind": "any",
            }],
        )
        agent = create_persona_agent(
            agent_id="ember-owl", config=config, llm_client=make_client(),
        )
        dispatcher = EventDispatcher()
        schedulers: dict = {}
        caches: dict[str, ScheduledWakesCache] = {}

        await initialize_persona_agents(
            {"ember-owl": agent}, dispatcher, schedulers,
            scheduled_wakes_caches=caches,
        )

        row = next(
            r for r in await caches["ember-owl"].list_timers()
            if r.timer_id == "frac"
        )
        assert row.interval_ms == 1501

        await schedulers["ember-owl"].stop()
        await caches["ember-owl"].close()
        await agent.close_memory()

    def test_half_millisecond_uses_bankers_rounding(self):
        """``round()`` is round-half-to-even, *not* round-half-up.

        PR 2.1 review (3): ``test_fractional_seconds_rounds_to_nearest_ms``
        deliberately picks ``1.501`` (an unambiguous fractional value) and
        so never visits the half-case branch. This test pins the actual
        ``round(seconds * 1000)`` semantics on values whose product with
        1000 is *exactly* ``N.5`` (verified exactly representable in IEEE-754),
        so a future refactor to ``math.floor`` / ``int`` / a manual
        round-half-up surfaces in CI:

        * ``1.5005s`` → ``1500.5`` → ``1500`` — the half rounds *down* to the
          even neighbour. Round-half-up would give ``1501``.
        * ``1.5015s`` → ``1501.5`` → ``1502`` — the half rounds *up* to the
          even neighbour. Truncation / ``math.floor`` would give ``1501``.

        Only round-half-to-even yields the pair ``(1500, 1502)``:
        round-half-up gives ``(1501, 1502)``; truncation gives ``(1500, 1501)``.
        Asserting both halves uniquely identifies banker's rounding.
        """
        # Guard the premise: both products must be exact halves, else the
        # test would be pinning float-representation noise, not the rule.
        assert 1.5005 * 1000 == 1500.5
        assert 1.5015 * 1000 == 1501.5

        rows = _build_scheduled_wake_rows(
            [
                {"id": "down", "interval_seconds": 1.5005, "kind": "any"},
                {"id": "up", "interval_seconds": 1.5015, "kind": "any"},
            ],
            now_ms=0,
            saved_anchors_ms={},
        )
        by_id = {r.timer_id: r for r in rows}
        assert by_id["down"].interval_ms == 1500  # half-to-even rounds down
        assert by_id["up"].interval_ms == 1502    # half-to-even rounds up


class TestCacheLifecycleOnSetupFailure:
    """Cache-setup failures (``initialize`` / ``list_timers`` /
    ``rebuild_from_config``) must close the cache *and* stop the scheduler
    before re-raising — mirroring the existing
    ``_register_configured_timers`` partial-init contract.  Without this,
    a SQL fault leaks the ``aiosqlite`` connection *and* leaves the
    started scheduler running with no entry in ``tick_schedulers`` for
    the server's shutdown path to find.

    Why: the original PR 2.1 only caught ``_register_configured_timers``
    failures, leaving an asymmetry — a register failure cleaned up both
    cache and scheduler; an earlier cache-setup failure cleaned up
    neither.  RFC 0024 PR 2.1 review follow-up.
    """

    async def test_rebuild_failure_closes_cache_and_stops_scheduler(
        self, tmp_path: Path, monkeypatch,
    ):
        """``rebuild_from_config`` is the most stateful cache-setup step
        — the connection is fully open by the time it runs.  A failure
        here is the canonical resource-leak risk; pin that ``close()``
        is called on the cache (doing real work, not the
        ``_conn is None`` short-circuit) and that ``stop()`` is called
        on the scheduler.
        """
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

        close_calls = 0
        original_close = ScheduledWakesCache.close

        async def _tracking_close(self):
            nonlocal close_calls
            close_calls += 1
            await original_close(self)

        async def _explode_rebuild(self, rows):
            raise RuntimeError("simulated rebuild failure")

        monkeypatch.setattr(ScheduledWakesCache, "close", _tracking_close)
        monkeypatch.setattr(
            ScheduledWakesCache, "rebuild_from_config", _explode_rebuild,
        )

        from agents.tick import TickScheduler
        stop_calls = 0
        original_stop = TickScheduler.stop

        async def _tracking_stop(self, *args, **kwargs):
            nonlocal stop_calls
            stop_calls += 1
            await original_stop(self, *args, **kwargs)

        monkeypatch.setattr(TickScheduler, "stop", _tracking_stop)

        with pytest.raises(RuntimeError, match="simulated rebuild failure"):
            await initialize_persona_agents(
                {"ember-owl": agent}, dispatcher, schedulers,
                scheduled_wakes_caches=caches,
            )

        # The connection was open when rebuild raised, so close() did
        # real work — exactly the leak the cleanup branch prevents.
        assert close_calls == 1, (
            f"expected exactly one cache.close() during cleanup; "
            f"got {close_calls}"
        )
        # Scheduler was started before init_persona_timers ran; without
        # the cleanup it stays alive with no entry in tick_schedulers
        # for the server's stop() to find.
        assert stop_calls == 1, (
            f"expected exactly one scheduler.stop() during cleanup; "
            f"got {stop_calls}"
        )
        # And no half-state leaks downstream into the caller's dicts.
        assert caches == {}
        assert schedulers == {}

        await agent.close_memory()

    async def test_initialize_failure_closes_cache_and_stops_scheduler(
        self, tmp_path: Path, monkeypatch,
    ):
        """``initialize`` is the earliest cache-setup step.  Even when
        it raises before ``_conn`` is set, the cleanup branch must call
        ``close()`` (no-op in that case — but the call itself proves the
        branch executed) and must stop the scheduler.  Pinned separately
        from the ``rebuild`` case because the two failure modes hit
        different points in the cache's own state machine.
        """
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

        close_calls = 0
        original_close = ScheduledWakesCache.close

        async def _tracking_close(self):
            nonlocal close_calls
            close_calls += 1
            await original_close(self)

        async def _explode_init(self):
            raise RuntimeError("simulated initialize failure")

        monkeypatch.setattr(ScheduledWakesCache, "close", _tracking_close)
        monkeypatch.setattr(ScheduledWakesCache, "initialize", _explode_init)

        from agents.tick import TickScheduler
        stop_calls = 0
        original_stop = TickScheduler.stop

        async def _tracking_stop(self, *args, **kwargs):
            nonlocal stop_calls
            stop_calls += 1
            await original_stop(self, *args, **kwargs)

        monkeypatch.setattr(TickScheduler, "stop", _tracking_stop)

        with pytest.raises(RuntimeError, match="simulated initialize failure"):
            await initialize_persona_agents(
                {"ember-owl": agent}, dispatcher, schedulers,
                scheduled_wakes_caches=caches,
            )

        assert close_calls == 1
        assert stop_calls == 1
        assert caches == {}
        assert schedulers == {}

        await agent.close_memory()
