"""Shared-pool metric attribute-schema test (RFC 0008 PR 4 N4 / PR 6b).

Pins the attribute keys carried on every emitted point of
``agent.shared_pool.{reads,writes,denied}`` so a silent attribute
rename (e.g. ``agent.id`` → ``agent_id``) breaks the build instead of
only surfacing in downstream dashboards.

Lives under ``tests/integration/`` because the test exercises a real
:class:`SharedMemoryPool` ACL gate end-to-end and validates the OTEL
counter round-trip via :class:`InMemoryMetricReader`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from agents.memory.shared_pool import (
    SharedMemoryPermissionError,
    SharedMemoryPool,
    SharedPoolConfig,
)
from agents.observability import metrics as pmetrics


@pytest.fixture
def metric_reader() -> Iterator[InMemoryMetricReader]:
    reader = InMemoryMetricReader()
    pmetrics.init_metrics(reader=reader)
    try:
        yield reader
    finally:
        asyncio.run(pmetrics.shutdown())


def _collect(reader: InMemoryMetricReader) -> dict[str, list[dict[str, Any]]]:
    """Return ``{metric_name: [attribute_dicts]}`` for every emitted point."""
    data = reader.get_metrics_data()
    out: dict[str, list[dict[str, Any]]] = {}
    if data is None:
        return out
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                pts = []
                for dp in getattr(m.data, "data_points", []):
                    pts.append(dict(dp.attributes))
                out[m.name] = pts
    return out


@pytest.mark.asyncio
async def test_shared_pool_metrics_emit_documented_attribute_schema(
    tmp_path: Path,
    metric_reader: InMemoryMetricReader,
) -> None:
    """Pin the documented attribute keys for each shared-pool counter.

    Pre-PR 6b the OTEL inventory only verified counters *existed*; this
    test additionally asserts the keys carried on every emitted point
    match the documented schema (PR 4 N4):

    * ``agent.shared_pool.reads``  → ``{pool, agent.id}``
    * ``agent.shared_pool.writes`` → ``{pool, agent.id}``
    * ``agent.shared_pool.denied`` → ``{pool, agent.id, operation}``
    """
    cfg = SharedPoolConfig(
        name="team-mem",
        readers=frozenset({"alice", "bob"}),
        writers=frozenset({"alice"}),
    )
    pool = SharedMemoryPool(cfg, db_path=str(tmp_path / "shared.db"))
    await pool.initialize()
    try:
        await pool.write("alice", "hello world", confidence=0.9)
        await pool.read("alice", "hello", limit=5)
        with pytest.raises(SharedMemoryPermissionError):
            await pool.write("bob", "denied write", confidence=0.5)
        with pytest.raises(SharedMemoryPermissionError):
            await pool.read("carol", "anything", limit=5)
    finally:
        await pool.close()

    points = _collect(metric_reader)

    write_attrs = points.get("agent.shared_pool.writes", [])
    assert write_attrs, "agent.shared_pool.writes counter did not emit"
    for attrs in write_attrs:
        assert set(attrs.keys()) == {"pool", "agent.id"}, (
            f"agent.shared_pool.writes attrs drifted: {attrs}"
        )
        assert attrs["pool"] == "team-mem"

    read_attrs = points.get("agent.shared_pool.reads", [])
    assert read_attrs, "agent.shared_pool.reads counter did not emit"
    for attrs in read_attrs:
        assert set(attrs.keys()) == {"pool", "agent.id"}, (
            f"agent.shared_pool.reads attrs drifted: {attrs}"
        )

    denied_attrs = points.get("agent.shared_pool.denied", [])
    assert denied_attrs, "agent.shared_pool.denied counter did not emit"
    operations_seen: set[str] = set()
    for attrs in denied_attrs:
        assert set(attrs.keys()) == {"pool", "agent.id", "operation"}, (
            f"agent.shared_pool.denied attrs drifted: {attrs}"
        )
        operations_seen.add(str(attrs["operation"]))
    assert operations_seen == {"read", "write"}, (
        f"denied counter must distinguish read vs write; got {operations_seen}"
    )
