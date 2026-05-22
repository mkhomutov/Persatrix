"""
Tests for RFC 0031 PR plan PR 4 review follow-up F2: the ``surface``
attribute on the ``sessions.writes`` counter must distinguish the four
persona-reachable write paths.

Before this change every counter increment going through
``EpisodicMemory.store_episode`` carried ``surface="episode"`` regardless
of whether the call came from a plain observation, a procedural-tier
write, or a shared-pool publish.  Only ``record_interaction`` set a
different surface (``"relationship"``).  An operator dashboarding by
``surface`` could not tell a procedural write from a pool publish from
a plain observation — the only signal was ``agent.id``, which collapses
to the pool name for shared-pool writes and to the persona id for
everything else.

The fix promotes ``surface`` to a kwarg on ``store_episode`` so each
calling layer pins it:

* ``MemoryStore.store_observation``       → ``surface="observation"``
* ``ProceduralFacadeMixin.store_procedure`` → ``surface="procedure"``
* ``SharedMemoryPool.write``               → ``surface="shared_pool"``
* direct ``EpisodicMemory.store_episode`` call (no facade) → ``"episode"``
* ``record_interaction``                   → ``"relationship"`` (unchanged)

The ``session_id`` cardinality story is unchanged — ``surface`` is a
small fixed enumeration (five values) so the additional dimension does
not blow up the time-series count.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from agents.memory.episodic import EpisodicMemory
from agents.memory.facade import MemoryStore
from agents.memory.shared_pool import (
    SharedMemoryPool,
    SharedPoolConfig,
    SharedPoolRegistry,
)
from agents.memory.shared_pool_facade import publish_via_facade
from agents.observability import metrics as pmetrics


@pytest_asyncio.fixture
async def metric_reader() -> AsyncIterator[InMemoryMetricReader]:
    reader = InMemoryMetricReader()
    pmetrics.init_metrics(reader=reader)
    try:
        yield reader
    finally:
        await pmetrics.shutdown()


def _collect_surfaces(
    reader: InMemoryMetricReader,
) -> list[tuple[int, dict[str, Any]]]:
    data = reader.get_metrics_data()
    if data is None:
        return []
    out: list[tuple[int, dict[str, Any]]] = []
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name != "sessions.writes":
                    continue
                for dp in getattr(m.data, "data_points", []):
                    out.append((int(dp.value), dict(dp.attributes)))
    return out


async def test_store_observation_emits_surface_observation(
    tmp_path: Path,
    metric_reader: InMemoryMetricReader,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PERSATRIX_SESSION_ID", raising=False)
    fac = MemoryStore(agent_id="ember-owl", db_path=str(tmp_path / "m.db"))
    await fac.initialize()
    try:
        await fac.store_observation("hello", session_id="run-a")
    finally:
        await fac.close()

    points = _collect_surfaces(metric_reader)
    matched = [
        attrs for _, attrs in points if attrs.get("session_id") == "run-a"
    ]
    assert matched, f"expected a write tagged run-a; got {points!r}"
    assert matched[0].get("surface") == "observation", (
        "store_observation must set surface='observation' so dashboards "
        f"split it from procedure / shared_pool / episode; got: {matched[0]!r}"
    )


async def test_store_procedure_emits_surface_procedure(
    tmp_path: Path,
    metric_reader: InMemoryMetricReader,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PERSATRIX_SESSION_ID", raising=False)
    fac = MemoryStore(agent_id="ember-owl", db_path=str(tmp_path / "m.db"))
    await fac.initialize()
    try:
        await fac.store_procedure(
            "deploy.rollback", "run `make rollback`",
            confidence=0.9, session_id="run-a",
        )
    finally:
        await fac.close()

    points = _collect_surfaces(metric_reader)
    matched = [
        attrs for _, attrs in points if attrs.get("session_id") == "run-a"
    ]
    assert matched, f"expected a write tagged run-a; got {points!r}"
    assert matched[0].get("surface") == "procedure", (
        "store_procedure must set surface='procedure' so dashboards split "
        f"it from observation / shared_pool / episode; got: {matched[0]!r}"
    )


async def test_shared_pool_write_emits_surface_shared_pool(
    tmp_path: Path,
    metric_reader: InMemoryMetricReader,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PERSATRIX_SESSION_ID", raising=False)
    cfg = SharedPoolConfig(
        name="team-mem",
        readers=frozenset({"alice"}),
        writers=frozenset({"alice"}),
    )
    pool = SharedMemoryPool(cfg, db_path=str(tmp_path / "shared.db"))
    await pool.initialize()
    try:
        await pool.write("alice", "team note", confidence=0.9, session_id="run-a")
    finally:
        await pool.close()

    points = _collect_surfaces(metric_reader)
    matched = [
        attrs for _, attrs in points if attrs.get("session_id") == "run-a"
    ]
    assert matched, f"expected a write tagged run-a; got {points!r}"
    assert matched[0].get("surface") == "shared_pool", (
        "SharedMemoryPool.write must set surface='shared_pool' so "
        "dashboards split it from observation / procedure / episode; "
        f"got: {matched[0]!r}"
    )


async def test_publish_via_facade_emits_surface_shared_pool(
    tmp_path: Path,
    metric_reader: InMemoryMetricReader,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # publish_via_facade routes through pool.write so it inherits its
    # surface automatically; this test pins the cross-module contract.
    monkeypatch.delenv("PERSATRIX_SESSION_ID", raising=False)
    cfg = SharedPoolConfig(
        name="team-mem",
        readers=frozenset({"alice"}),
        writers=frozenset({"alice"}),
    )
    pool = SharedMemoryPool(cfg, db_path=str(tmp_path / "shared.db"))
    await pool.initialize()
    registry = SharedPoolRegistry({"team-mem": pool})
    try:
        await publish_via_facade(
            registry, "alice", "team-mem", "shared note",
            confidence=0.9, session_id="run-a",
        )
    finally:
        await pool.close()

    points = _collect_surfaces(metric_reader)
    matched = [
        attrs for _, attrs in points if attrs.get("session_id") == "run-a"
    ]
    assert matched, f"expected a write tagged run-a; got {points!r}"
    assert matched[0].get("surface") == "shared_pool", (
        f"publish_via_facade must surface as 'shared_pool'; "
        f"got: {matched[0]!r}"
    )


async def test_direct_store_episode_keeps_episode_default(
    tmp_path: Path,
    metric_reader: InMemoryMetricReader,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The default surface remains "episode" so any future caller that
    # reaches store_episode directly (without going through a layered
    # surface) still shows up on the counter — just without a more
    # specific surface label.  This is the lower bound; the facade
    # callers above are the upper layer that sets a richer label.
    monkeypatch.delenv("PERSATRIX_SESSION_ID", raising=False)
    mem = EpisodicMemory(agent_id="ember-owl", db_path=str(tmp_path / "m.db"))
    await mem.initialize()
    try:
        await mem.store_episode("hi", {}, session_id="run-a")
    finally:
        await mem.close()

    points = _collect_surfaces(metric_reader)
    matched = [
        attrs for _, attrs in points if attrs.get("session_id") == "run-a"
    ]
    assert matched, f"expected a write tagged run-a; got {points!r}"
    assert matched[0].get("surface") == "episode", (
        "direct store_episode call must keep the default surface='episode' "
        f"so unlabeled callers remain queryable; got: {matched[0]!r}"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
