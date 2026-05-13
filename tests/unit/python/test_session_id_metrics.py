"""
Tests for the RFC 0031 Phase 1 ``sessions.writes`` counter.

The counter mirrors the orchestrator-side ``sessions.writes`` instrument
(see :file:`internal/observability/metrics/channel_instruments.go`) and
increments once per ``EpisodicMemory.store_episode`` /
``RelationshipMemory.record_interaction`` call.  Operators dashboard the
counter to confirm the env-var session id flows through to disk without
running the manual MT-SESSION-001 raw-SQLite asserts.

The metric name intentionally **omits** the ``agent.`` prefix used by
every other Python instrument so that a single PromQL query covers both
binaries (see ``agents/observability/metrics.py`` for the rationale).
PR 4 review follow-up F1.

Closes RFC 0031 PR plan PR 4 finding #4 (telemetry counter promised in
the PR 3 plan but not shipped).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from agents.memory.episodic import EpisodicMemory
from agents.memory.relationship import RelationshipMemory
from agents.observability import metrics as pmetrics


@pytest_asyncio.fixture
async def metric_reader() -> AsyncIterator[InMemoryMetricReader]:
    # PR 4 review follow-up F9: async fixture so teardown awaits
    # ``pmetrics.shutdown()`` directly instead of spinning a fresh event
    # loop via ``asyncio.run`` from sync teardown — the latter is brittle
    # when other tests in the suite hold long-lived background tasks on a
    # different loop (the OTEL SDK's metric provider stores an event-loop
    # reference internally on registration).
    reader = InMemoryMetricReader()
    pmetrics.init_metrics(reader=reader)
    try:
        yield reader
    finally:
        await pmetrics.shutdown()


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
    writes = points.get("sessions.writes", [])
    assert writes, (
        "sessions.writes counter did not emit on store_episode; "
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
    writes = points.get("sessions.writes", [])
    assert writes, "counter must emit on record_interaction"
    by_session = {attrs.get("session_id"): v for v, attrs in writes}
    assert by_session.get("run-a") == 1, (
        f"expected one write tagged run-a; got: {writes!r}"
    )


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
    writes = points.get("sessions.writes", [])
    by_session = {attrs.get("session_id"): v for v, attrs in writes}
    assert by_session.get("legacy") == 1, (
        f"unset session_id must surface as 'legacy' on the counter; "
        f"got: {writes!r}"
    )


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
    writes = points.get("sessions.writes", [])
    by_session: dict[str, int] = {}
    for v, attrs in writes:
        by_session.setdefault(attrs.get("session_id", "<none>"), 0)
        by_session[attrs.get("session_id", "<none>")] += v
    assert by_session.get("run-a") == 2
    assert by_session.get("run-b") == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
