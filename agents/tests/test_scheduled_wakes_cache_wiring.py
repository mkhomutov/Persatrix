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
