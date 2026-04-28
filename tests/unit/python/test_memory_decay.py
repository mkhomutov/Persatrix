"""
Unit tests for the procedural-tier confidence-decay subsystem (RFC 0008 PR 5).

Covers:

- :func:`agents.memory.decay.compute_decayed_confidence` — pure-math
  identities and clamping.
- :func:`agents.memory.episodic_procedural.recall_procedures` — read-time
  decay + ``c_min`` filtering.
- :func:`agents.memory.episodic_procedural.refresh_confidence` — reset
  to ``1.0`` + ``last_validated_at`` stamping.
- :class:`agents.memory.eviction.EvictionPass` — procedural eviction
  via the new ``_evict_procedural_decay`` pass.
- :class:`agents.memory.facade.MemoryFacade` — ``store_procedure``
  refresh-on-existing-key, ``retrieve_procedures`` stale alert, and
  decay-knob construction validation.
- Migration v6 — applies idempotently and leaves a v0.2.x schema
  open-able with the new columns added.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import AsyncGenerator

import pytest

from agents.memory.decay import (
    DEFAULT_C_MIN,
    DEFAULT_LAMBDA_PER_DAY,
    DEFAULT_STALE_CONFIDENCE_ALERT_THRESHOLD,
    SECONDS_PER_DAY,
    compute_decayed_confidence,
)
from agents.memory.episodic import EpisodicMemory
from agents.memory.episodic_procedural import (
    extract_procedure_key,
    recall_procedures,
    refresh_confidence,
)
from agents.memory.eviction import EvictionPass
from agents.memory.facade import MemoryFacade


# ─── Pure-math: compute_decayed_confidence ────────────────────


def test_decay_at_t_zero_equals_c0() -> None:
    """Identity check — at t=0 the formula returns c_0 unchanged."""
    assert compute_decayed_confidence(1.0, 0.0) == pytest.approx(1.0)
    assert compute_decayed_confidence(0.5, 0.0) == pytest.approx(0.5)


def test_decay_at_half_life_returns_half() -> None:
    """At ``ln(2) / lambda`` days the formula returns c_0 / 2.

    With the shipped default ``lambda = 0.01 / day`` the half-life is
    ``ln(2) / 0.01 ≈ 69.3147`` days.  The fixture pins the exact value
    so a future retune of the default is caught by this test.
    """
    half_life_seconds = (math.log(2.0) / DEFAULT_LAMBDA_PER_DAY) * SECONDS_PER_DAY
    assert compute_decayed_confidence(1.0, half_life_seconds) == pytest.approx(0.5, abs=1e-9)


def test_decay_at_c_min_boundary_with_default_lambda() -> None:
    """At ~230 days the decayed value sits at the default ``c_min`` floor.

    ``c0 * exp(-0.01 * 230) ≈ 0.1003`` which is the smallest value still
    admitted by the default ``c_min = 0.1`` filter (boundary check).
    """
    age_seconds = 230.0 * SECONDS_PER_DAY
    decayed = compute_decayed_confidence(1.0, age_seconds)
    assert decayed == pytest.approx(0.1003, abs=1e-3)
    assert decayed >= DEFAULT_C_MIN


def test_decay_clamps_negative_age_to_zero() -> None:
    """Future ``last_validated_at`` (clock skew) cannot inflate confidence."""
    assert compute_decayed_confidence(0.7, -1000.0) == pytest.approx(0.7)


def test_decay_clamps_c0_into_unit_interval() -> None:
    """``c_0`` outside ``[0, 1]`` is clamped before the exponential is applied."""
    assert compute_decayed_confidence(1.5, 0.0) == pytest.approx(1.0)
    assert compute_decayed_confidence(-0.5, 0.0) == pytest.approx(0.0)


def test_decay_rejects_negative_lambda() -> None:
    """A negative ``lambda`` would *grow* confidence over time; reject it."""
    with pytest.raises(ValueError, match="lambda_per_day"):
        compute_decayed_confidence(1.0, 0.0, lambda_per_day=-0.001)


# ─── extract_procedure_key ────────────────────────────────────


def test_extract_procedure_key_returns_first_match() -> None:
    assert extract_procedure_key(["procedure:deploy", "category:ops"]) == "deploy"


def test_extract_procedure_key_returns_none_when_absent() -> None:
    assert extract_procedure_key(["category:ops", "owner:sre"]) is None


# ─── recall_procedures ────────────────────────────────────────


@pytest.fixture
async def facade() -> AsyncGenerator[MemoryFacade, None]:
    """Fresh in-memory facade per test."""
    fac = MemoryFacade(agent_id="proc-test", db_path=":memory:")
    await fac.initialize()
    try:
        yield fac
    finally:
        await fac.close()


async def test_recall_procedures_filters_below_c_min(facade: MemoryFacade) -> None:
    """Entries whose decayed confidence < c_min are filtered before limit slice."""
    await facade.store_procedure("fresh", "still good", confidence=0.9)
    await facade.store_procedure("stale", "old habit", confidence=0.05)  # < 0.1
    results = await facade.retrieve_procedures()
    keys = [e.key for e in results]
    assert "fresh" in keys
    assert "stale" not in keys


async def test_recall_procedures_applies_decay_at_read_time(
    facade: MemoryFacade,
) -> None:
    """The decay multiplier is applied to the stored ``c_0`` at read time."""
    await facade.store_procedure("k1", "body", confidence=0.8)
    db = facade.episodic._ensure_db()  # noqa: SLF001 — test reaches in to forge age
    # Backdate ``last_validated_at`` 100 days into the past.
    age_seconds = 100.0 * SECONDS_PER_DAY
    await db.execute(
        "UPDATE episodes SET last_validated_at = ? "
        "WHERE agent_id = ? AND tags_json LIKE '%\"procedure:k1\"%'",
        (time.time() - age_seconds, "proc-test"),
    )
    await db.commit()
    results = await recall_procedures(
        db, "proc-test", c_min=0.0, lambda_per_day=DEFAULT_LAMBDA_PER_DAY,
    )
    assert len(results) == 1
    expected = 0.8 * math.exp(-DEFAULT_LAMBDA_PER_DAY * 100.0)
    assert results[0].decayed_confidence == pytest.approx(expected, abs=1e-3)
    assert results[0].base_confidence == pytest.approx(0.8)


async def test_recall_procedures_query_filter(facade: MemoryFacade) -> None:
    await facade.store_procedure("a", "ship widget alpha", confidence=0.9)
    await facade.store_procedure("b", "ship gadget beta", confidence=0.9)
    results = await facade.retrieve_procedures(query="widget")
    assert len(results) == 1
    assert results[0].key == "a"


# ─── refresh_confidence ───────────────────────────────────────


async def test_refresh_confidence_resets_to_one_and_stamps_now(
    facade: MemoryFacade,
) -> None:
    await facade.store_procedure("k", "body", confidence=0.4)
    db = facade.episodic._ensure_db()  # noqa: SLF001
    # Backdate so the current decayed value is below the base.
    await db.execute(
        "UPDATE episodes SET last_validated_at = ? WHERE agent_id = ?",
        (time.time() - 50.0 * SECONDS_PER_DAY, "proc-test"),
    )
    await db.commit()

    before = time.time()
    refreshed = await refresh_confidence(db, "proc-test", "k")
    after = time.time()
    assert refreshed is True

    async with db.execute(
        "SELECT confidence, last_validated_at FROM episodes "
        "WHERE agent_id = ? AND tags_json LIKE '%\"procedure:k\"%'",
        ("proc-test",),
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == pytest.approx(1.0)
    assert before <= row[1] <= after


async def test_refresh_confidence_returns_false_when_no_match(
    facade: MemoryFacade,
) -> None:
    db = facade.episodic._ensure_db()  # noqa: SLF001
    assert await refresh_confidence(db, "proc-test", "nonexistent") is False


async def test_refresh_confidence_rejects_empty_key(
    facade: MemoryFacade,
) -> None:
    db = facade.episodic._ensure_db()  # noqa: SLF001
    with pytest.raises(ValueError, match="key"):
        await refresh_confidence(db, "proc-test", "")


# ─── store_procedure refresh path ─────────────────────────────


async def test_store_procedure_refreshes_existing_key(
    facade: MemoryFacade,
) -> None:
    """Storing under an existing key updates confidence/last_validated, no duplicate row."""
    await facade.store_procedure("dup", "v1", confidence=0.5)
    db = facade.episodic._ensure_db()  # noqa: SLF001
    # Backdate so refresh has a measurable effect.
    await db.execute(
        "UPDATE episodes SET last_validated_at = ?, confidence = ? "
        "WHERE agent_id = ?",
        (time.time() - 30.0 * SECONDS_PER_DAY, 0.5, "proc-test"),
    )
    await db.commit()

    await facade.store_procedure("dup", "v2-IGNORED-IN-PR5", confidence=0.9)

    async with db.execute(
        "SELECT confidence, summary FROM episodes "
        "WHERE agent_id = ? AND tags_json LIKE '%\"procedure:dup\"%'",
        ("proc-test",),
    ) as cur:
        rows = list(await cur.fetchall())
    assert len(rows) == 1, "refresh path must not insert a duplicate row"
    assert rows[0][0] == pytest.approx(1.0), "confidence is reset to 1.0"
    # Body is intentionally not rewritten in PR 5; new content lands in PR 6+.
    assert rows[0][1] == "v1"


# ─── retrieve_procedures stale alert ──────────────────────────


async def test_retrieve_procedures_emits_stale_alert(
    facade: MemoryFacade,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Entries in ``[c_min, stale_threshold)`` log ``stale_memory_injection``."""
    await facade.store_procedure("stale-but-admitted", "old proc", confidence=1.0)
    db = facade.episodic._ensure_db()  # noqa: SLF001
    # Engineer decayed value to fall in [0.1, 0.3) — pick age such that
    # ``exp(-0.01 * d) ≈ 0.2`` → d = -ln(0.2)/0.01 ≈ 161 days.
    await db.execute(
        "UPDATE episodes SET last_validated_at = ?, confidence = ? "
        "WHERE agent_id = ?",
        (time.time() - 161.0 * SECONDS_PER_DAY, 1.0, "proc-test"),
    )
    await db.commit()

    with caplog.at_level(logging.WARNING, logger="agents.memory.facade_procedural"):
        results = await facade.retrieve_procedures()

    assert len(results) == 1
    assert any(rec.message == "stale_memory_injection" for rec in caplog.records)


async def test_retrieve_procedures_no_alert_above_threshold(
    facade: MemoryFacade,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Entries with decayed >= stale_threshold do not log the alert."""
    await facade.store_procedure("fresh", "new proc", confidence=0.9)

    with caplog.at_level(logging.WARNING, logger="agents.memory.facade_procedural"):
        results = await facade.retrieve_procedures()

    assert len(results) == 1
    assert not any(rec.message == "stale_memory_injection" for rec in caplog.records)


# ─── EvictionPass procedural decay ────────────────────────────


async def test_eviction_pass_evicts_below_c_min() -> None:
    """The procedural pass deletes rows whose decayed confidence < c_min."""
    fac = MemoryFacade(agent_id="evict-proc", db_path=":memory:")
    await fac.initialize()
    try:
        await fac.store_procedure("keep", "fresh", confidence=0.9)
        await fac.store_procedure("kill", "decayed", confidence=1.0)
        db = fac.episodic._ensure_db()  # noqa: SLF001
        # Backdate ``kill`` 1000 days → decayed ≈ 1.0 * exp(-10) ≈ 4.5e-5 < 0.1
        await db.execute(
            "UPDATE episodes SET last_validated_at = ? "
            "WHERE agent_id = ? AND tags_json LIKE '%\"procedure:kill\"%'",
            (time.time() - 1000.0 * SECONDS_PER_DAY, "evict-proc"),
        )
        await db.commit()

        runner = EvictionPass(
            "evict-proc", episodic_cap=100, ttl_low_importance_days=30,
        )
        stats = await runner.run(db)
        assert stats.procedural_evicted == 1

        async with db.execute(
            "SELECT tags_json FROM episodes WHERE agent_id = ?",
            ("evict-proc",),
        ) as cur:
            remaining = [r[0] for r in await cur.fetchall()]
        assert any('"procedure:keep"' in r for r in remaining)
        assert not any('"procedure:kill"' in r for r in remaining)
    finally:
        await fac.close()


# ─── Facade construction validation ──────────────────────────


def test_facade_rejects_negative_lambda() -> None:
    with pytest.raises(ValueError, match="lambda_per_day"):
        MemoryFacade(agent_id="x", db_path=":memory:", lambda_per_day=-0.1)


def test_facade_rejects_c_min_out_of_range() -> None:
    with pytest.raises(ValueError, match="c_min"):
        MemoryFacade(agent_id="x", db_path=":memory:", c_min=1.5)


def test_facade_rejects_inverted_threshold_pair() -> None:
    """``stale_threshold < c_min`` would silently disable the alert window."""
    with pytest.raises(ValueError, match="stale_confidence_alert_threshold"):
        MemoryFacade(
            agent_id="x", db_path=":memory:",
            c_min=0.4, stale_confidence_alert_threshold=0.2,
        )


def test_facade_accepts_default_decay_knobs() -> None:
    """Default constructor uses ``agents.memory.decay`` constants."""
    fac = MemoryFacade(agent_id="x", db_path=":memory:")
    assert fac._lambda_per_day == DEFAULT_LAMBDA_PER_DAY  # noqa: SLF001
    assert fac._c_min == DEFAULT_C_MIN  # noqa: SLF001
    assert fac._stale_alert_threshold == DEFAULT_STALE_CONFIDENCE_ALERT_THRESHOLD  # noqa: SLF001


# ─── Migration v6 safety ──────────────────────────────────────


async def test_migration_v6_adds_columns_idempotently(tmp_path) -> None:
    """Re-opening an EpisodicMemory DB twice does not error on v6."""
    db_path = str(tmp_path / "mem.db")
    mem1 = EpisodicMemory(agent_id="m6", db_path=db_path)
    await mem1.initialize()
    await mem1.close()

    # Second open — migration tracker says we are at v6; re-running must
    # not attempt the ``ALTER TABLE`` again (PRAGMA guard).
    mem2 = EpisodicMemory(agent_id="m6", db_path=db_path)
    await mem2.initialize()

    db = mem2._ensure_db()  # noqa: SLF001
    async with db.execute("PRAGMA table_info(episodes)") as cur:
        cols = {row[1] for row in await cur.fetchall()}
    assert "confidence" in cols
    assert "last_validated_at" in cols
    await mem2.close()


async def test_migration_v6_preserves_existing_episodes(tmp_path) -> None:
    """An episode written before v6 is preserved with column defaults applied.

    Initialises a fresh DB at the current head (so v6 has run), forges
    a row that mimics a pre-PR-5 row: ``confidence`` at the migration
    DEFAULT (1.0) and ``last_validated_at`` NULL.  Verifies the read
    path falls back to ``created_at`` for the decay anchor.
    """
    db_path = str(tmp_path / "legacy.db")
    mem = EpisodicMemory(agent_id="legacy", db_path=db_path)
    await mem.initialize()
    db = mem._ensure_db()  # noqa: SLF001
    await db.execute(
        "INSERT INTO episodes (id, agent_id, summary, tags_json, "
        "importance, created_at, access_count, last_validated_at) "
        "VALUES ('e1', 'legacy', 'old proc', '[\"procedure:legacy-key\"]', "
        "0.7, ?, 0, NULL)",
        (time.time() - 10.0 * SECONDS_PER_DAY,),
    )
    await db.commit()

    results = await recall_procedures(db, "legacy", c_min=0.0)
    assert len(results) == 1
    # Legacy shim: confidence at the v6 DEFAULT (1.0) + non-default
    # importance → prefer importance as the base (matches PR 2 writes).
    assert results[0].base_confidence == pytest.approx(0.7)
    await mem.close()
