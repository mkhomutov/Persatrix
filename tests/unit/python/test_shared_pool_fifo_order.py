"""Deterministic shared-pool FIFO eviction — :mod:`agents.memory.shared_pool`.

Guards the ``rowid`` insertion-order tiebreak on the ``created_at DESC`` keep-set
in :meth:`SharedMemoryPool._enforce_fifo_cap` (``agents/memory/shared_pool.py``).
That subquery selects the newest ``max_entries`` rows to KEEP; everything else is
``DELETE``d. Without the tiebreak, ``created_at`` ties (entries published in one
instant under the eval driver's FrozenClock) leave *which* rows survive the
destructive cap SQLite-implementation-defined — non-portable for RFC 0044 goldens
and a nondeterministic data loss. ``episodes.id`` is a random uuid4, so ``rowid``
(never ``WITHOUT ROWID``) — not ``id`` — is the portable tiebreak.

Split into its own file (mirrors ``test_fact_store_recall_order.py``) to keep
``test_shared_memory_pool.py`` under the 500-line size gate.
"""

from __future__ import annotations

from typing import Any

from agents.memory.shared_pool import SharedMemoryPool, SharedPoolConfig

# ─── Deterministic FIFO tiebreak ────────────────────────────


async def test_fifo_eviction_equal_created_at_deterministic(tmp_path: Any) -> None:
    """When ``created_at`` ties, the FIFO cap keeps the most-recently-inserted
    ``max_entries`` rows — the ``rowid`` (insertion-order) tiebreak on the
    ``created_at DESC`` keep-set.

    Five rows are seeded (``rowid`` = insertion order), their ``created_at`` is
    forged to one instant (mirrors the FrozenClock stamping a batch), then the
    cap is enforced. With ``max_entries=3`` the three newest-inserted must
    survive and the two oldest must be evicted — deterministically."""
    cfg = SharedPoolConfig(
        name="tiny",
        readers=frozenset({"alice"}),
        writers=frozenset({"alice"}),
        max_entries=3,
    )
    p = SharedMemoryPool(cfg, db_path=str(tmp_path / "tie.db"))
    await p.initialize()
    try:
        # Seed 5 rows directly (rowid = insertion order); store_episode does not
        # trigger the cap, so all five are present when we forge the tie.
        for i in range(5):
            await p._episodic.store_episode(  # noqa: SLF001 — seed pool rows
                summary=f"entry-{i}", context={},
            )
        db = p._episodic._ensure_db()  # noqa: SLF001
        pool_agent = p._episodic._agent_id  # noqa: SLF001
        await db.execute(  # forge a created_at tie across all five rows
            "UPDATE episodes SET created_at = 1000.0 WHERE agent_id = ?",
            (pool_agent,),
        )
        await db.commit()

        await p._enforce_fifo_cap()  # noqa: SLF001 — exercise the fixed keep-set

        async with db.execute(
            "SELECT summary FROM episodes WHERE agent_id = ? ORDER BY summary",
            (pool_agent,),
        ) as cur:
            survivors = [r[0] for r in await cur.fetchall()]
        assert survivors == ["entry-2", "entry-3", "entry-4"], (
            "equal-created_at FIFO cap must keep the most-recently-inserted "
            "max_entries rows (rowid DESC tiebreak), not an implementation-"
            "defined subset"
        )
    finally:
        await p.close()
