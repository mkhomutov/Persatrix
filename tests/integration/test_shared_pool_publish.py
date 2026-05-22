"""End-to-end shared-pool publish (RFC 0008 PR plan PR 4).

Three agents — writer A, reader B, denied C — share one
SharedMemoryPool.  Verifies:

* A publishes 3 entries via :meth:`MemoryStore.publish_to_pool`.
* B retrieves all 3 via :meth:`MemoryStore.read_from_pool`.
* C is denied on both read and write paths.
* The original isolated entry on A's MemoryStore survives the publish
  (the publish path is *additive* — RFC 0008 §H "curated").
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest

from agents.memory.facade import MemoryStore
from agents.memory.shared_pool import (
    SharedMemoryPermissionError,
    SharedMemoryPool,
    SharedPoolConfig,
    SharedPoolRegistry,
)


@pytest.fixture
async def world(
    tmp_path: Any,
) -> AsyncGenerator[
    tuple[MemoryStore, MemoryStore, MemoryStore, SharedMemoryPool], None,
]:
    db = str(tmp_path / "world.db")
    cfg = SharedPoolConfig(
        name="team-knowledge",
        readers=frozenset({"agent-a", "agent-b"}),
        writers=frozenset({"agent-a"}),
        max_entries=50,
    )
    pool = SharedMemoryPool(cfg, db_path=db)
    await pool.initialize()
    registry = SharedPoolRegistry({"team-knowledge": pool})

    async def _facade(agent_id: str) -> MemoryStore:
        f = MemoryStore(
            agent_id=agent_id, db_path=db, shared_pools=registry,
        )
        await f.initialize()
        return f

    a = await _facade("agent-a")
    b = await _facade("agent-b")
    c = await _facade("agent-c")
    try:
        yield a, b, c, pool
    finally:
        await a.close()
        await b.close()
        await c.close()
        await pool.close()


async def test_publish_then_read_three_agents(
    world: tuple[MemoryStore, MemoryStore, MemoryStore, SharedMemoryPool],
) -> None:
    a, b, c, _pool = world

    # A publishes three curated insights.
    await a.publish_to_pool(
        "team-knowledge", "deployment uses blue green on tuesdays", confidence=0.9,
    )
    await a.publish_to_pool(
        "team-knowledge", "deployment rollbacks gated on canary latency",
        confidence=0.8,
    )
    await a.publish_to_pool(
        "team-knowledge", "deployment feature flags read at request boundary",
        confidence=0.7,
    )

    # B retrieves all three (FTS5 AND on the shared "deployment" token).
    entries = await b.read_from_pool("team-knowledge", "deployment")
    assert len(entries) == 3
    sources = {e.source_agent for e in entries}
    assert sources == {"agent-a"}
    contents = {e.content for e in entries}
    assert any("blue green" in s for s in contents)

    # C is denied on read.
    with pytest.raises(SharedMemoryPermissionError) as exc_read:
        await c.read_from_pool("team-knowledge", "anything")
    assert exc_read.value.reason == "not_in_readers"

    # C is also denied on write (through the publish path).
    with pytest.raises(SharedMemoryPermissionError) as exc_write:
        await c.publish_to_pool(
            "team-knowledge", "evil claim", confidence=0.5,
        )
    assert exc_write.value.reason == "not_in_writers"


async def test_publish_does_not_consume_isolated_entry(
    world: tuple[MemoryStore, MemoryStore, MemoryStore, SharedMemoryPool],
) -> None:
    a, _b, _c, _pool = world

    # A stores an isolated observation, then publishes its content.
    await a.store_observation(
        "internal note about alpha service", importance=0.7,
    )
    await a.publish_to_pool(
        "team-knowledge", "internal note about alpha service",
        confidence=0.7,
    )

    # The isolated entry is still recallable on A's own facade.
    isolated = await a.retrieve_relevant("alpha service", limit=5)
    assert any("alpha service" in e.content for e in isolated)


async def test_min_confidence_filter_at_facade(
    world: tuple[MemoryStore, MemoryStore, MemoryStore, SharedMemoryPool],
) -> None:
    a, b, _c, _pool = world

    await a.publish_to_pool("team-knowledge", "weakly trusted claim", confidence=0.4)
    await a.publish_to_pool("team-knowledge", "strongly trusted claim", confidence=0.9)

    high = await b.read_from_pool(
        "team-knowledge", "trusted claim", min_confidence=0.8,
    )
    contents = {e.content for e in high}
    assert "strongly trusted claim" in contents
    assert "weakly trusted claim" not in contents
