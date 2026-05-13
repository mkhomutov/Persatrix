"""
Tests for the RFC 0031 Phase 1 ``agent.sessions.writes`` counter.

The counter mirrors the orchestrator-side ``sessions.writes`` instrument
(see :file:`internal/observability/metrics/channel_instruments.go`) and
increments once per ``EpisodicMemory.store_episode`` /
``RelationshipMemory.record_interaction`` call.  Operators dashboard the
counter to confirm the env-var session id flows through to disk without
running the manual MT-SESSION-001 raw-SQLite asserts.

Closes RFC 0031 PR plan PR 4 finding #4 (telemetry counter promised in
the PR 3 plan but not shipped).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from agents.memory.episodic import EpisodicMemory
from agents.memory.relationship import RelationshipMemory
from agents.observability import metrics as pmetrics


@pytest.fixture
def metric_reader() -> Iterator[InMemoryMetricReader]:
    reader = InMemoryMetricReader()
    pmetrics.init_metrics(reader=reader)
    try:
        yield reader
    finally:
        asyncio.run(pmetrics.shutdown())


def _collect(reader: InMemoryMetricReader) -> dict[str, list[tuple[int, dict[str, Any]]]]:
    """Return ``{metric_name: [(value, attribute_dict), ...]}``."""
    data = reader.get_metrics_data()
    out: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    if data is None:
        return out
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                pts = []
                for dp in getattr(m.data, "data_points", []):
                    pts.append((int(dp.value), dict(dp.attributes)))
                out[m.name] = pts
    return out


@pytest.mark.asyncio
async def test_store_episode_increments_sessions_writes(
    tmp_path: Path,
    metric_reader: InMemoryMetricReader,
) -> None:
    mem = EpisodicMemory(agent_id="ember-owl", db_path=str(tmp_path / "m.db"))
    await mem.initialize()
    try:
        await mem.store_episode("hello", {}, session_id="run-a")
    finally:
        await mem.close()

    points = _collect(metric_reader)
    writes = points.get("agent.sessions.writes", [])
    assert writes, (
        "agent.sessions.writes counter did not emit on store_episode; "
        "RFC 0031 PR plan PR 4 finding #4 — must increment once per write"
    )
    matching = [
        (v, attrs) for v, attrs in writes
        if attrs.get("session_id") == "run-a"
    ]
    assert matching, (
        f"counter must carry session_id=run-a attribute; got {writes!r}"
    )
    total = sum(v for v, _ in matching)
    assert total == 1, f"expected exactly 1 write under run-a; got {total}"


@pytest.mark.asyncio
async def test_record_interaction_increments_sessions_writes(
    tmp_path: Path,
    metric_reader: InMemoryMetricReader,
) -> None:
    rel = RelationshipMemory(agent_id="ember-owl", db_path=str(tmp_path / "m.db"))
    await rel.initialize()
    try:
        await rel.record_interaction("bob", "chat", session_id="run-a")
    finally:
        await rel.close()

    points = _collect(metric_reader)
    writes = points.get("agent.sessions.writes", [])
    assert writes, "counter must emit on record_interaction"
    by_session = {attrs.get("session_id"): v for v, attrs in writes}
    assert by_session.get("run-a") == 1, (
        f"expected one write tagged run-a; got: {writes!r}"
    )


@pytest.mark.asyncio
async def test_default_legacy_attribute_is_carried(
    tmp_path: Path,
    metric_reader: InMemoryMetricReader,
) -> None:
    mem = EpisodicMemory(agent_id="ember-owl", db_path=str(tmp_path / "m.db"))
    await mem.initialize()
    try:
        await mem.store_episode("hello", {})
    finally:
        await mem.close()

    points = _collect(metric_reader)
    writes = points.get("agent.sessions.writes", [])
    by_session = {attrs.get("session_id"): v for v, attrs in writes}
    assert by_session.get("legacy") == 1, (
        f"unset session_id must surface as 'legacy' on the counter; "
        f"got: {writes!r}"
    )


@pytest.mark.asyncio
async def test_multiple_writes_partition_by_session_id(
    tmp_path: Path,
    metric_reader: InMemoryMetricReader,
) -> None:
    mem = EpisodicMemory(agent_id="ember-owl", db_path=str(tmp_path / "m.db"))
    await mem.initialize()
    try:
        await mem.store_episode("a1", {}, session_id="run-a")
        await mem.store_episode("a2", {}, session_id="run-a")
        await mem.store_episode("b1", {}, session_id="run-b")
    finally:
        await mem.close()

    points = _collect(metric_reader)
    writes = points.get("agent.sessions.writes", [])
    by_session: dict[str, int] = {}
    for v, attrs in writes:
        by_session.setdefault(attrs.get("session_id", "<none>"), 0)
        by_session[attrs.get("session_id", "<none>")] += v
    assert by_session.get("run-a") == 2
    assert by_session.get("run-b") == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
