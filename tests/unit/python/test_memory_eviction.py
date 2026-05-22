"""
Unit tests for ``agents.memory.eviction`` (RFC 0008 PR plan PR 2a).

Covers:

- TTL eviction: low-importance entries past the window are deleted; high-
  importance entries are retained.
- Size-cap eviction: lowest-scoring excess entries are deleted; deterministic
  tie-break by ``created_at ASC``.
- Eviction loop: failures log a warning and the loop survives.
- ``MemoryStore`` lifecycle: the eviction task is started by
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
from agents.memory.facade import MemoryStore

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
    """Loop logs a warning on a failed pass and resumes on the next tick.

    PR #221 deep-review L-7: the previous version of this test only
    cancelled the loop and never injected a failure, so the survives-
    failure contract documented in [docs/rfcs/0008-pr-plan.md] was not
    actually exercised.  We monkeypatch :meth:`EvictionPass.run` to raise
    on the first invocation and succeed on the second, then assert the
    warning landed in ``caplog`` *and* that a follow-up pass executed.
    """
    db = episodic._ensure_db()  # noqa: SLF001

    call_count = 0
    original_run = EvictionPass.run

    async def flaky_run(
        self: EvictionPass, conn: object,
    ) -> EvictionStats:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("injected eviction failure")
        return await original_run(self, conn)  # type: ignore[arg-type]

    caplog.set_level("WARNING", logger="agents.memory.eviction")
    monkeypatched = EvictionPass.run
    EvictionPass.run = flaky_run  # type: ignore[method-assign, assignment]
    try:
        task = asyncio.create_task(
            eviction_loop(
                "evict-test", db,
                episodic_cap=1000,
                ttl_low_importance_days=30,
                cadence_seconds=0.02,
            ),
        )
        # Allow at least two ticks: one that raises, one that succeeds.
        for _ in range(50):
            if call_count >= 2:
                break
            await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        EvictionPass.run = monkeypatched  # type: ignore[method-assign]

    assert call_count >= 2, (
        "loop did not run a second pass after the first one raised"
    )
    failure_warnings = [
        rec for rec in caplog.records
        if rec.levelname == "WARNING"
        and "Eviction pass failed" in rec.getMessage()
    ]
    assert failure_warnings, (
        "expected a warning log when the eviction pass raised"
    )


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


async def test_eviction_loop_startup_pass_before_full_cadence(
    episodic: EpisodicMemory,
) -> None:
    """First pass fires after min(60, cadence/10), not the full cadence.

    PR 2a M-2: without the startup-delay fix, an agent with the default
    1-hour cadence stays over-cap for a full hour after restart.  Here we
    use a 0.5 s cadence so the expected startup delay is
    min(60, 0.05) = 0.05 s; we wait 4× that delay (0.2 s) — well below
    the full cadence — and assert the first pass has already run.
    """
    db = episodic._ensure_db()  # noqa: SLF001
    cadence_seconds = 0.5
    startup_delay = min(60.0, cadence_seconds / 10.0)  # 0.05 s

    call_count = 0
    original_run = EvictionPass.run

    async def counting_run(
        self: EvictionPass, conn: object,
    ) -> EvictionStats:
        nonlocal call_count
        call_count += 1
        return await original_run(self, conn)  # type: ignore[arg-type]

    EvictionPass.run = counting_run  # type: ignore[method-assign, assignment]
    try:
        task = asyncio.create_task(
            eviction_loop(
                "evict-test", db,
                episodic_cap=100,
                ttl_low_importance_days=30,
                cadence_seconds=cadence_seconds,
            ),
        )
        # Wait 4× startup delay (0.20 s) — only 40% of the full
        # cadence (0.5 s), so without the M-2 fix the first pass would
        # not have fired yet and the assertion below would fail.
        for _ in range(4):
            if call_count >= 1:
                break
            await asyncio.sleep(startup_delay)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        EvictionPass.run = original_run  # type: ignore[method-assign]

    assert call_count >= 1, (
        f"first pass did not fire before full cadence ({cadence_seconds}s); "
        f"expected startup delay of {startup_delay:.3f}s"
    )


# ─── MemoryStore integration ─────────────────────────────────


async def test_facade_starts_and_cancels_eviction_task() -> None:
    fac = MemoryStore(
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
    fac = MemoryStore(agent_id="scope-test", db_path=":memory:")
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
    stats = EvictionStats(
        ttl_evicted=0, cap_evicted=0, total_after=0, procedural_evicted=0,
    )
    with pytest.raises(Exception):  # noqa: BLE001 — frozen-dataclass error
        stats.ttl_evicted = 1  # type: ignore[misc]


def test_eviction_stats_procedural_evicted_is_required() -> None:
    """PR 6b deep-review Should-Fix #3: pin the intentional removal of
    the ``procedural_evicted`` default value (PR 5 R2 N2).

    Without this pin a future contributor restoring the default would
    only break the documented intent — every eviction call site must
    populate ``procedural_evicted`` explicitly so the field cannot
    silently read as ``0`` when an actual count is missing.
    """
    with pytest.raises(TypeError):
        EvictionStats(  # type: ignore[call-arg]
            ttl_evicted=0, cap_evicted=0, total_after=0,
        )


# ─── MemoryStore __init__ validation (PR #221 deep-review M1) ────────


def test_facade_rejects_invalid_episodic_cap() -> None:
    """MemoryStore.__init__ must reject episodic_cap < 1 immediately."""
    with pytest.raises(ValueError, match="episodic_cap"):
        MemoryStore(agent_id="a", db_path=":memory:", episodic_cap=0)


def test_facade_rejects_invalid_ttl() -> None:
    """MemoryStore.__init__ must reject ttl_low_importance_days < 1 immediately."""
    with pytest.raises(ValueError, match="ttl_low_importance_days"):
        MemoryStore(
            agent_id="a", db_path=":memory:", ttl_low_importance_days=0,
        )


def test_facade_rejects_non_positive_cadence() -> None:
    """MemoryStore.__init__ must reject eviction_cadence_seconds <= 0 immediately."""
    with pytest.raises(ValueError, match="eviction_cadence_seconds"):
        MemoryStore(
            agent_id="a", db_path=":memory:", eviction_cadence_seconds=0,
        )


# ─── Procedural-tier separation (PR #221 review M-1) ─────────────────


async def test_ttl_skips_procedure_rows(episodic: EpisodicMemory) -> None:
    """Low-confidence procedure rows are NOT TTL-evicted.

    RFC 0008 §G separates episodic eviction from procedural confidence
    decay (the latter lands in PR 5).  ``MemoryStore.store_procedure``
    persists procedures as episode rows tagged ``procedure:{key}`` with
    ``confidence`` mapped onto ``importance``; without the procedure
    guard in :mod:`agents.memory.eviction`, a stale low-confidence
    procedure would be silently TTL-evicted by the episodic policy.
    """
    db = episodic._ensure_db()  # noqa: SLF001
    old_ts = time.time() - 31 * 86400.0
    # An old, low-importance procedure (would otherwise satisfy the TTL
    # predicate).  Tag format mirrors ``MemoryStore.store_procedure``.
    await db.execute(
        "INSERT INTO episodes (id, agent_id, summary, importance, "
        "tags_json, created_at, compression_level) "
        "VALUES (?, ?, ?, ?, ?, ?, 0)",
        (
            "proc-old", "evict-test", "how to deploy",
            0.2, '["procedure:deploy"]', old_ts,
        ),
    )
    await db.commit()

    runner = EvictionPass(
        "evict-test", episodic_cap=1000, ttl_low_importance_days=30,
    )
    stats = await runner.run(db)
    assert stats.ttl_evicted == 0
    async with db.execute(
        "SELECT id FROM episodes WHERE agent_id = ?", ("evict-test",),
    ) as cur:
        rows = await cur.fetchall()
    assert {r[0] for r in rows} == {"proc-old"}


async def test_size_cap_skips_procedure_rows(
    episodic: EpisodicMemory,
) -> None:
    """Procedure rows are excluded from both the size-cap budget and
    the candidate set.

    Setup: 3 procedures + 3 episodic entries, ``episodic_cap=2``.  The
    cap governs only the 3 episodic rows so exactly 1 episodic entry is
    evicted; the 3 procedures survive untouched and ``total_after``
    reports the evictable (episodic) count = 2.
    """
    db = episodic._ensure_db()  # noqa: SLF001
    for i in range(3):
        # Set ``confidence`` explicitly to match ``importance`` so the
        # PR #225 review S2 legacy-row shim in
        # ``episodic_procedural._resolve_base_confidence`` does not
        # interpret these as pre-PR-5 rows and hand a low decay base
        # to ``_evict_procedural_decay``.  The test's intent is "procs
        # are excluded from size-cap eviction", not decay eviction.
        await db.execute(
            "INSERT INTO episodes (id, agent_id, summary, importance, "
            "tags_json, created_at, compression_level, confidence) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
            (
                f"proc-{i}", "evict-test", f"procedure-{i}",
                0.9, f'["procedure:k{i}"]', time.time(), 0.9,
            ),
        )
    await db.commit()
    for i in range(3):
        await episodic.store_episode(
            summary=f"ep-{i}", context={}, importance=0.1 * i,
        )

    runner = EvictionPass(
        "evict-test", episodic_cap=2, ttl_low_importance_days=365,
    )
    stats = await runner.run(db)
    assert stats.cap_evicted == 1
    assert stats.total_after == 2  # episodic-only count
    async with db.execute(
        "SELECT id FROM episodes WHERE agent_id = ? ORDER BY id",
        ("evict-test",),
    ) as cur:
        rows = await cur.fetchall()
    surviving = {r[0] for r in rows}
    # All 3 procedure rows survive; 2 of the 3 episodic rows survive.
    assert {"proc-0", "proc-1", "proc-2"}.issubset(surviving)
    assert sum(1 for r in surviving if not r.startswith("proc-")) == 2
