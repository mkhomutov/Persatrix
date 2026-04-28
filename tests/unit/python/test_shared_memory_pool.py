"""Unit tests for ``agents.memory.shared_pool`` (RFC 0008 PR plan PR 4).

Covers:

* Reader / writer ACL enforcement (deny-by-default).
* Provenance: framework-injected ``source_agent``, caller spoof rejected
  via the facade boundary.
* ``min_confidence`` consumer-side trust filter.
* Sensitive-pool isolation (RFC 0008 §H safety constraint #3).
* FIFO eviction at ``max_entries``.
* ``required_confidence`` enforcement.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest

from agents.memory.facade import MemoryFacade
from agents.memory.shared_pool import (
    SharedMemoryPermissionError,
    SharedMemoryPool,
    SharedPoolConfig,
    SharedPoolRegistry,
    build_registry_from_config,
)


# ─── Fixtures ────────────────────────────────────────────────────


@pytest.fixture
async def pool(tmp_path: Any) -> AsyncGenerator[SharedMemoryPool, None]:
    db = str(tmp_path / "shared.db")
    cfg = SharedPoolConfig(
        name="team-knowledge",
        readers=frozenset({"alice", "bob"}),
        writers=frozenset({"alice"}),
        max_entries=100,
        required_confidence=0.0,
        sensitive=False,
    )
    p = SharedMemoryPool(cfg, db_path=db)
    await p.initialize()
    try:
        yield p
    finally:
        await p.close()


# ─── ACL ────────────────────────────────────────────────────────


async def test_reader_in_acl_can_read(pool: SharedMemoryPool) -> None:
    await pool.write("alice", "shared insight one", confidence=0.7)
    entries = await pool.read("bob", "shared")
    assert len(entries) == 1
    assert entries[0].content == "shared insight one"
    assert entries[0].source_agent == "alice"
    assert entries[0].confidence == pytest.approx(0.7)


async def test_reader_not_in_acl_denied(pool: SharedMemoryPool) -> None:
    with pytest.raises(SharedMemoryPermissionError) as exc:
        await pool.read("carol", "anything")
    assert exc.value.reason == "not_in_readers"


async def test_writer_in_acl_can_write(pool: SharedMemoryPool) -> None:
    entry_id = await pool.write("alice", "hello", confidence=0.5)
    assert entry_id


async def test_writer_not_in_acl_denied(pool: SharedMemoryPool) -> None:
    with pytest.raises(SharedMemoryPermissionError) as exc:
        await pool.write("bob", "should fail", confidence=0.5)
    assert exc.value.reason == "not_in_writers"


# ─── Provenance + validation ────────────────────────────────────


async def test_source_agent_is_framework_injected(pool: SharedMemoryPool) -> None:
    await pool.write("alice", "from alice", confidence=0.5)
    entries = await pool.read("alice", "from")
    assert entries[0].source_agent == "alice"


async def test_publish_rejects_caller_provenance_spoof(tmp_path: Any) -> None:
    """``MemoryFacade.publish_to_pool`` must not let callers spoof source_agent.

    The facade enforces this by *always* passing
    ``source_agent_override=self.agent_id`` and exposing no parameter for
    the caller to set.  The pool's underlying API accepts an override
    (used by the facade) but the facade boundary keeps it inaccessible to
    user code.
    """
    db = str(tmp_path / "spoof.db")
    cfg = SharedPoolConfig(
        name="pool",
        readers=frozenset({"alice"}),
        writers=frozenset({"alice"}),
    )
    pool_inst = SharedMemoryPool(cfg, db_path=db)
    await pool_inst.initialize()
    registry = SharedPoolRegistry({"pool": pool_inst})
    facade = MemoryFacade(
        agent_id="alice", db_path=db, shared_pools=registry,
    )
    await facade.initialize()
    try:
        await facade.publish_to_pool(
            "pool", "claim", confidence=0.5,
        )
        # The facade has no source_agent kwarg — confirm the only path
        # in is the framework-injected one.
        import inspect
        sig = inspect.signature(MemoryFacade.publish_to_pool)
        assert "source_agent" not in sig.parameters
        assert "source_agent_override" not in sig.parameters
    finally:
        await facade.close()
        await pool_inst.close()


async def test_confidence_out_of_range_rejected(pool: SharedMemoryPool) -> None:
    with pytest.raises(ValueError):
        await pool.write("alice", "x", confidence=1.5)
    with pytest.raises(ValueError):
        await pool.write("alice", "x", confidence=-0.1)


async def test_required_confidence_floor(tmp_path: Any) -> None:
    cfg = SharedPoolConfig(
        name="strict",
        readers=frozenset({"alice"}),
        writers=frozenset({"alice"}),
        required_confidence=0.7,
    )
    p = SharedMemoryPool(cfg, db_path=str(tmp_path / "s.db"))
    await p.initialize()
    try:
        with pytest.raises(ValueError):
            await p.write("alice", "weak", confidence=0.5)
        await p.write("alice", "strong", confidence=0.9)
    finally:
        await p.close()


async def test_min_confidence_filter(pool: SharedMemoryPool) -> None:
    await pool.write("alice", "weakly supported claim", confidence=0.4)
    await pool.write("alice", "strongly supported claim", confidence=0.9)
    all_entries = await pool.read("alice", "supported claim")
    assert len(all_entries) == 2
    filtered = await pool.read(
        "alice", "supported claim", min_confidence=0.7,
    )
    assert len(filtered) == 1
    assert filtered[0].content == "strongly supported claim"


# ─── Sensitive pool ─────────────────────────────────────────────


async def test_sensitive_pool_blocks_publish(tmp_path: Any) -> None:
    db = str(tmp_path / "sens.db")
    cfg = SharedPoolConfig(
        name="secrets",
        readers=frozenset({"alice"}),
        writers=frozenset({"alice"}),
        sensitive=True,
    )
    pool_inst = SharedMemoryPool(cfg, db_path=db)
    await pool_inst.initialize()
    registry = SharedPoolRegistry({"secrets": pool_inst})
    facade = MemoryFacade(
        agent_id="alice", db_path=db, shared_pools=registry,
    )
    await facade.initialize()
    try:
        with pytest.raises(SharedMemoryPermissionError) as exc:
            await facade.publish_to_pool("secrets", "leak", confidence=0.9)
        assert exc.value.reason == "sensitive_pool_isolation"
        # Direct pool.write still works — only the publish path is gated.
        await pool_inst.write("alice", "internal", confidence=0.9)
    finally:
        await facade.close()
        await pool_inst.close()


# ─── FIFO eviction ──────────────────────────────────────────────


async def test_fifo_eviction_at_cap(tmp_path: Any) -> None:
    cfg = SharedPoolConfig(
        name="tiny",
        readers=frozenset({"alice"}),
        writers=frozenset({"alice"}),
        max_entries=3,
    )
    p = SharedMemoryPool(cfg, db_path=str(tmp_path / "tiny.db"))
    await p.initialize()
    try:
        ids: list[str] = []
        for i in range(5):
            ids.append(
                await p.write("alice", f"entry-{i}", confidence=0.5),
            )
        entries = await p.read("alice", "entry")
        # max_entries=3 so the two oldest (entry-0, entry-1) must be gone.
        contents = {e.content for e in entries}
        assert "entry-0" not in contents
        assert "entry-1" not in contents
        assert "entry-4" in contents
        assert len(contents) == 3
    finally:
        await p.close()


# ─── Registry ───────────────────────────────────────────────────


async def test_registry_unknown_pool_raises(tmp_path: Any) -> None:
    db = str(tmp_path / "r.db")
    facade = MemoryFacade(
        agent_id="alice",
        db_path=db,
        shared_pools=SharedPoolRegistry({}),
    )
    await facade.initialize()
    try:
        with pytest.raises(SharedMemoryPermissionError) as exc:
            await facade.publish_to_pool("nope", "x", confidence=0.5)
        assert exc.value.reason == "unknown_pool"
        with pytest.raises(SharedMemoryPermissionError) as exc:
            await facade.read_from_pool("nope", "x")
        assert exc.value.reason == "unknown_pool"
    finally:
        await facade.close()


async def test_facade_without_registry_denies_pool_calls(tmp_path: Any) -> None:
    facade = MemoryFacade(
        agent_id="alice", db_path=str(tmp_path / "n.db"),
    )
    await facade.initialize()
    try:
        with pytest.raises(SharedMemoryPermissionError) as exc:
            await facade.publish_to_pool("any", "x", confidence=0.5)
        assert exc.value.reason == "unknown_pool"
    finally:
        await facade.close()


def test_build_registry_from_config(tmp_path: Any) -> None:
    raw = {
        "team-knowledge": {
            "readers": ["alice", "bob"],
            "writers": ["alice"],
            "max_entries": 500,
            "required_confidence": 0.5,
            "sensitive": False,
        },
    }
    registry = build_registry_from_config(raw, db_path=str(tmp_path / "x.db"))
    assert registry.names() == ("team-knowledge",)
    pool_inst = registry.get("team-knowledge")
    assert pool_inst.config.max_entries == 500
    assert "alice" in pool_inst.config.writers


def test_build_registry_empty() -> None:
    registry = build_registry_from_config(None, db_path=":memory:")
    assert registry.names() == ()


def test_pool_config_validation() -> None:
    with pytest.raises(ValueError):
        SharedPoolConfig(name="")
    with pytest.raises(ValueError):
        SharedPoolConfig(name="x", max_entries=0)
    with pytest.raises(ValueError):
        SharedPoolConfig(name="x", required_confidence=1.5)
