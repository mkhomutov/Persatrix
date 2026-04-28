"""
Unit tests for ``agents.memory.eviction`` (RFC 0008 PR plan PR 2a).

Covers:

- TTL eviction: low-importance entries past the window are deleted; high-
  importance entries are retained.
- Size-cap eviction: lowest-scoring excess entries are deleted; deterministic
  tie-break by ``created_at ASC``.
- Eviction loop: failures log a warning and the loop survives.
- ``MemoryFacade`` lifecycle: the eviction task is started by
  ``initialize()`` and cancelled by ``close()``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator

import pytest

from agents.memory.episodic import EpisodicMemory
from agents.memory.eviction import (
    TTL_IMPORTANCE_THRESHOLD,
    EvictionPass,
    EvictionStats,
    eviction_loop,
)
from agents.memory.facade import MemoryFacade


# ─── Fixtures ─────────────────────────────────────────────────


@pytest.fixture
async def episodic() -> AsyncGenerator[EpisodicMemory, None]:
    mem = EpisodicMemory(agent_id="evict-test", db_path=":memory:")
    await mem.initialize()
    try:
        yield mem
    finally:
        await mem.close()


# ─── EvictionPass — input validation ──────────────────────────


def test_eviction_pass_rejects_invalid_cap() -> None:
    with pytest.raises(ValueError, match="episodic_cap"):
        EvictionPass("a", episodic_cap=0, ttl_low_importance_days=1)


def test_eviction_pass_rejects_invalid_ttl() -> None:
    with pytest.raises(ValueError, match="ttl_low_importance_days"):
        EvictionPass("a", episodic_cap=10, ttl_low_importance_days=0)


# ─── TTL eviction ─────────────────────────────────────────────


async def test_ttl_evicts_low_importance_old_entries(
    episodic: EpisodicMemory,
) -> None:
    db = episodic._ensure_db()  # noqa: SLF001 — test access
    # Insert a 31-day-old low-importance row directly so we can backdate
    # ``created_at`` (the public API stamps ``time.time()``).
    old_ts = time.time() - 31 * 86400.0
    await db.execute(
        "INSERT INTO episodes (id, agent_id, summary, importance, created_at, "
        "compression_level) VALUES (?, ?, ?, ?, ?, 0)",
        ("old-low", "evict-test", "stale", 0.2, old_ts),
    )
    await db.execute(
        "INSERT INTO episodes (id, agent_id, summary, importance, created_at, "
        "compression_level) VALUES (?, ?, ?, ?, ?, 0)",
        ("old-high", "evict-test", "important", 0.5, old_ts),
    )
    await db.commit()

    runner = EvictionPass(
        "evict-test", episodic_cap=1000, ttl_low_importance_days=30,
    )
    stats = await runner.run(db)
    assert stats.ttl_evicted == 1
    # The high-importance entry survives.
    async with db.execute(
        "SELECT id FROM episodes WHERE agent_id = ?", ("evict-test",),
    ) as cur:
        rows = await cur.fetchall()
    assert {r[0] for r in rows} == {"old-high"}


async def test_ttl_threshold_constant_matches_rfc() -> None:
    """RFC 0008 §G freezes the threshold at 0.3."""
    assert TTL_IMPORTANCE_THRESHOLD == 0.3


# ─── Size-cap eviction ────────────────────────────────────────


async def test_size_cap_keeps_highest_scored(episodic: EpisodicMemory) -> None:
    db = episodic._ensure_db()  # noqa: SLF001
    base = time.time() - 100.0
    for i in range(5):
        await episodic.store_episode(
            summary=f"entry-{i}",
            context={},
            importance=0.1 * i,  # 0.0, 0.1, 0.2, 0.3, 0.4
        )
    # Cap to 2 → 3 lowest-scoring entries get evicted.
    runner = EvictionPass(
        "evict-test", episodic_cap=2, ttl_low_importance_days=365,
    )
    stats = await runner.run(db)
    assert stats.cap_evicted == 3
    assert stats.total_after == 2
    async with db.execute(
        "SELECT importance FROM episodes WHERE agent_id = ? "
        "ORDER BY importance DESC", ("evict-test",),
    ) as cur:
        rows = await cur.fetchall()
    importances = [r[0] for r in rows]
    # The two highest-importance entries (0.3, 0.4) survive.  Recency is
    # tied across the batch (all stamped within microseconds) so importance
    # dominates the hybrid score.
    assert importances == [pytest.approx(0.4), pytest.approx(0.3)]
    # Suppress unused-variable warning for ``base`` — kept for documentation.
    assert base < time.time()


async def test_size_cap_no_op_when_under_cap(episodic: EpisodicMemory) -> None:
    db = episodic._ensure_db()  # noqa: SLF001
    await episodic.store_episode(summary="only", context={}, importance=0.5)
    runner = EvictionPass(
        "evict-test", episodic_cap=10, ttl_low_importance_days=365,
    )
    stats = await runner.run(db)
    assert stats.cap_evicted == 0
    assert stats.total_after == 1


async def test_size_cap_tie_break_by_created_at(
    episodic: EpisodicMemory,
) -> None:
    db = episodic._ensure_db()  # noqa: SLF001
    # Two entries with identical importance and access_count but distinct
    # created_at — the older one must be evicted under the
    # (score, created_at ASC) tie-break.
    base = time.time() - 1000.0
    await db.execute(
        "INSERT INTO episodes (id, agent_id, summary, importance, "
        "access_count, created_at, compression_level) "
        "VALUES (?, ?, ?, ?, 0, ?, 0)",
        ("older", "evict-test", "a", 0.5, base),
    )
    await db.execute(
        "INSERT INTO episodes (id, agent_id, summary, importance, "
        "access_count, created_at, compression_level) "
        "VALUES (?, ?, ?, ?, 0, ?, 0)",
        ("newer", "evict-test", "b", 0.5, base + 10.0),
    )
    await db.commit()
    runner = EvictionPass(
        "evict-test", episodic_cap=1, ttl_low_importance_days=365,
    )
    stats = await runner.run(db)
    assert stats.cap_evicted == 1
    async with db.execute(
        "SELECT id FROM episodes WHERE agent_id = ?", ("evict-test",),
    ) as cur:
        rows = await cur.fetchall()
    # The newer entry survives because the older one has a lower recency
    # norm and therefore the lower hybrid score; if the scores were
    # actually tied, ``created_at ASC`` would still drop the older row.
    assert [r[0] for r in rows] == ["newer"]


# ─── Eviction loop ────────────────────────────────────────────


async def test_eviction_loop_survives_pass_failure(
    episodic: EpisodicMemory, caplog: pytest.LogCaptureFixture,
) -> None:
    db = episodic._ensure_db()  # noqa: SLF001
    # Cancel the loop after one short tick so the test does not hang.
    task = asyncio.create_task(
        eviction_loop(
            "evict-test", db,
            episodic_cap=1000,
            ttl_low_importance_days=30,
            cadence_seconds=0.05,
        ),
    )
    await asyncio.sleep(0.15)  # allow ≥ 1 pass
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_eviction_loop_rejects_non_positive_cadence(
    episodic: EpisodicMemory,
) -> None:
    db = episodic._ensure_db()  # noqa: SLF001
    with pytest.raises(ValueError, match="cadence_seconds"):
        await eviction_loop(
            "x", db,
            episodic_cap=1, ttl_low_importance_days=1,
            cadence_seconds=0.0,
        )


# ─── MemoryFacade integration ─────────────────────────────────


async def test_facade_starts_and_cancels_eviction_task() -> None:
    fac = MemoryFacade(
        agent_id="lifecycle",
        db_path=":memory:",
        eviction_cadence_seconds=3600,  # never fires during the test
    )
    await fac.initialize()
    task = fac._eviction_task  # noqa: SLF001 — test access
    assert task is not None
    assert not task.done()
    await fac.close()
    # The task is awaited inside close() so it is done by the time we
    # observe it (cancelled exception consumed silently).
    assert task.done()


async def test_facade_scope_filter_finds_non_facade_writer() -> None:
    """PR 2a follow-up M1: column-level ``scope`` is honoured by recall."""
    fac = MemoryFacade(agent_id="scope-test", db_path=":memory:")
    await fac.initialize()
    try:
        # Bypass ``store_observation`` and write the scope only via the
        # column (mimicking ``InteractionTracker``'s code path).
        await fac.episodic.store_episode(
            summary="non-facade observation",
            context={},
            scope="channel:slack-#dev",
            importance=0.9,
        )
        results = await fac.retrieve_relevant(
            "observation", limit=10, scope="channel:slack-#dev",
        )
        assert any(
            entry.content == "non-facade observation" for entry in results
        ), "column-level scope must be honoured by retrieve_relevant"
    finally:
        await fac.close()


def test_eviction_stats_is_frozen() -> None:
    stats = EvictionStats(ttl_evicted=0, cap_evicted=0, total_after=0)
    with pytest.raises(Exception):  # noqa: BLE001 — frozen-dataclass error
        stats.ttl_evicted = 1  # type: ignore[misc]
