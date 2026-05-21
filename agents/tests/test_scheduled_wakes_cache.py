"""Unit tests for ``agents.memory.scheduled_wakes`` — RFC 0024 PR 2.

The ``scheduled_wakes`` SQLite table is a per-agent *derived cache* —
:file:`config/agents.yaml` is the source of truth and the cache is
rebuilt from config on every agent startup.  The table exists so a
restart mid-jitter-window does not fire the timer immediately; the
``next_fire_at_ms`` column carries the monotonic anchor.

Tests pin the four contract pieces named in :doc:`RFC 0024 §OQ §1
<../../docs/rfcs/0024-event-driven-scheduling>`:

* Rebuild from config replaces every row.
* Removed-from-config rows are deleted on rebuild (orphan cleanup).
* The ``source`` column reserves the runtime-mutation hook without
  shipping it now.
* Schema columns are typed exactly as the implementation contract
  specifies so future migrations have a stable starting point.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agents.memory.scheduled_wakes import (
    ScheduledWakeRow,
    ScheduledWakesCache,
)


@pytest.fixture
async def cache(tmp_path: Path):
    db_path = tmp_path / "scheduled_wakes.db"
    cache = ScheduledWakesCache(db_path=str(db_path), agent_id="ember-owl")
    await cache.initialize()
    try:
        yield cache
    finally:
        await cache.close()


class TestSchema:
    async def test_list_returns_empty_when_no_timers(
        self, cache: ScheduledWakesCache,
    ):
        """``initialize()`` creates the table; an empty cache yields ``[]``
        rather than raising — the on-disk schema is wired correctly."""
        assert await cache.list_timers() == []

    async def test_initialize_idempotent(self, cache: ScheduledWakesCache):
        """A second ``initialize()`` call is a no-op (the fixture already
        called it).  Tolerates the multi-agent shared-DB race where two
        agents' caches both call ``CREATE TABLE IF NOT EXISTS`` on
        startup."""
        await cache.initialize()
        assert await cache.list_timers() == []


class TestRebuildFromConfig:
    """Per RFC 0024 §OQ §1: ``agents.yaml`` is canonical, the table is a
    derived cache rebuilt on every startup."""

    async def test_rebuild_inserts_configured_timers(
        self, cache: ScheduledWakesCache,
    ):
        rows = [
            ScheduledWakeRow(
                timer_id="memory_consolidation",
                kind="memory_consolidation",
                interval_ms=60_000,
                jitter_ms=5_000,
                next_fire_at_ms=12_345,
            ),
            ScheduledWakeRow(
                timer_id="periodic_reflection",
                kind="reflection",
                interval_ms=300_000,
                jitter_ms=0,
                next_fire_at_ms=67_890,
            ),
        ]
        await cache.rebuild_from_config(rows)

        loaded = await cache.list_timers()
        loaded_by_id = {r.timer_id: r for r in loaded}
        assert set(loaded_by_id) == {"memory_consolidation", "periodic_reflection"}
        assert loaded_by_id["memory_consolidation"].kind == "memory_consolidation"
        assert loaded_by_id["memory_consolidation"].interval_ms == 60_000
        assert loaded_by_id["memory_consolidation"].jitter_ms == 5_000
        assert loaded_by_id["memory_consolidation"].source == "config"

    async def test_rebuild_deletes_orphans(self, cache: ScheduledWakesCache):
        """A timer removed from ``agents.yaml`` must be gone after the next
        rebuild — the cache cannot drift from config."""
        first = [
            ScheduledWakeRow(
                timer_id="memory_consolidation",
                kind="memory_consolidation",
                interval_ms=60_000,
                jitter_ms=0,
                next_fire_at_ms=0,
            ),
            ScheduledWakeRow(
                timer_id="to_be_removed",
                kind="any",
                interval_ms=120_000,
                jitter_ms=0,
                next_fire_at_ms=0,
            ),
        ]
        await cache.rebuild_from_config(first)
        assert {r.timer_id for r in await cache.list_timers()} == {
            "memory_consolidation",
            "to_be_removed",
        }

        # Operator removed `to_be_removed` from agents.yaml — restart rebuilds.
        await cache.rebuild_from_config([first[0]])
        loaded = await cache.list_timers()
        assert {r.timer_id for r in loaded} == {"memory_consolidation"}

    async def test_rebuild_empty_clears_table(self, cache: ScheduledWakesCache):
        """An empty config (no timers) clears every previously-cached row.

        Matches the v0.3.3-default `timers: []` shape — stock personas ship
        with no timers, so the cache must agree on first startup.
        """
        await cache.rebuild_from_config([
            ScheduledWakeRow(
                timer_id="legacy",
                kind="any",
                interval_ms=60_000,
                jitter_ms=0,
                next_fire_at_ms=0,
            ),
        ])
        await cache.rebuild_from_config([])
        assert await cache.list_timers() == []


class TestPerAgentIsolation:
    """The same SQLite file can host multiple agents' caches without rows
    bleeding across the ``agent_id`` boundary — required because the shared
    ``data/memory.db`` is the v0.3.x convention."""

    async def test_two_agents_isolated(self, tmp_path: Path):
        db_path = tmp_path / "shared.db"
        cache_a = ScheduledWakesCache(db_path=str(db_path), agent_id="ember-owl")
        cache_b = ScheduledWakesCache(db_path=str(db_path), agent_id="iron-fox")
        await cache_a.initialize()
        await cache_b.initialize()
        try:
            await cache_a.rebuild_from_config([
                ScheduledWakeRow(
                    timer_id="agent-a-timer",
                    kind="any",
                    interval_ms=60_000,
                    jitter_ms=0,
                    next_fire_at_ms=0,
                ),
            ])
            await cache_b.rebuild_from_config([
                ScheduledWakeRow(
                    timer_id="agent-b-timer",
                    kind="any",
                    interval_ms=60_000,
                    jitter_ms=0,
                    next_fire_at_ms=0,
                ),
            ])

            assert {r.timer_id for r in await cache_a.list_timers()} == {
                "agent-a-timer",
            }
            assert {r.timer_id for r in await cache_b.list_timers()} == {
                "agent-b-timer",
            }
        finally:
            await cache_a.close()
            await cache_b.close()


class TestSourceColumnReservation:
    """The ``source`` column reserves the runtime-mutation hook (RFC 0024
    §OQ §1 — "defer that decision until a use case appears") without
    shipping it in Phase 2.  Config-loaded rows are always ``source='config'``;
    no API to insert ``source='runtime'`` rows exists yet."""

    async def test_config_rows_marked_source_config(
        self, cache: ScheduledWakesCache,
    ):
        await cache.rebuild_from_config([
            ScheduledWakeRow(
                timer_id="t1",
                kind="any",
                interval_ms=60_000,
                jitter_ms=0,
                next_fire_at_ms=0,
            ),
        ])
        loaded = await cache.list_timers()
        assert loaded[0].source == "config"
