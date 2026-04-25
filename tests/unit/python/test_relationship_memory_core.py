"""
Tests for RelationshipMemory — per-agent-pair trust and interaction tracking.

All tests use in-memory SQLite (:memory:) for isolation and speed.
"""

import pytest

from agents.memory.relationship import (
    Interaction,
    RelationshipMemory,
    RelationshipSummary,
    _DEFAULT_TRUST,
    _MAX_RECENT_INTERACTIONS,
    _MAX_TRUST_DELTA,
)


# ─── Fixtures ───────────────────────────────────────────────


@pytest.fixture
async def memory():
    """Create an initialized RelationshipMemory instance with in-memory DB."""
    mem = RelationshipMemory(agent_id="test-agent", db_path=":memory:")
    await mem.initialize()
    yield mem
    await mem.close()


@pytest.fixture
async def memory_pair():
    """Two RelationshipMemory instances with different agent IDs on the same DB."""
    import os
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        mem_a = RelationshipMemory(agent_id="agent-a", db_path=path)
        mem_b = RelationshipMemory(agent_id="agent-b", db_path=path)
        await mem_a.initialize()
        await mem_b.initialize()
        yield mem_a, mem_b
        await mem_a.close()
        await mem_b.close()
    finally:
        os.unlink(path)


# ─── Dataclass tests ────────────────────────────────────────


class TestDataclasses:
    def test_interaction_fields(self):
        i = Interaction(
            id="i1",
            participant_id="a",
            participant_type="agent",
            other_participant_id="b",
            other_participant_type="agent",
            interaction_type="task_delegation",
            outcome="success",
            sentiment=0.8,
            created_at=1000.0,
        )
        assert i.interaction_type == "task_delegation"
        assert i.sentiment == 0.8

    def test_relationship_summary_defaults(self):
        s = RelationshipSummary(
            other_participant_id="b",
            other_participant_type="agent",
            trust_score=0.5,
            interaction_count=0,
            last_interaction_at=None,
            notes=None,
        )
        assert s.recent_interactions == []


# ─── Initialization ─────────────────────────────────────────


class TestInitialization:
    async def test_initialize_creates_tables(self, memory):
        """Migration v3 creates relationships and interactions tables."""
        db = memory._ensure_db()
        # Check relationships table exists
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='relationships'"
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None

        # Check interactions table exists
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='interactions'"
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None

    async def test_migration_v3_recorded(self, memory):
        db = memory._ensure_db()
        async with db.execute(
            "SELECT version FROM schema_version WHERE version = 3"
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None

    async def test_not_initialized_raises(self):
        mem = RelationshipMemory(agent_id="test", db_path=":memory:")
        with pytest.raises(RuntimeError, match="not initialized"):
            await mem.get_trust("other")

    async def test_close_idempotent(self, memory):
        await memory.close()
        await memory.close()  # Should not raise

    async def test_double_initialize_no_leak(self):
        """Calling initialize() twice closes the previous connection."""
        mem = RelationshipMemory(agent_id="test", db_path=":memory:")
        await mem.initialize()
        # Seed a relationship on first connection.
        await mem.update_trust("bob", delta=0.1, reason="r1")
        # Re-initialize — old connection should be closed, new one opened.
        # In-memory DB is lost, so bob should not exist.
        await mem.initialize()
        trust = await mem.get_trust("bob")
        assert trust == _DEFAULT_TRUST
        await mem.close()


# ─── Trust CRUD ──────────────────────────────────────────────


class TestGetTrust:
    async def test_unknown_agent_returns_default(self, memory):
        trust = await memory.get_trust("unknown-agent")
        assert trust == _DEFAULT_TRUST

    async def test_returns_stored_trust(self, memory):
        await memory.update_trust("bob", delta=0.1, reason="good work")
        trust = await memory.get_trust("bob")
        assert trust == pytest.approx(0.6, abs=0.001)


class TestUpdateTrust:
    async def test_positive_delta(self, memory):
        new_trust = await memory.update_trust("bob", delta=0.1, reason="good work")
        assert new_trust == pytest.approx(0.6, abs=0.001)

    async def test_negative_delta(self, memory):
        new_trust = await memory.update_trust("bob", delta=-0.15, reason="missed deadline")
        assert new_trust == pytest.approx(0.35, abs=0.001)

    async def test_delta_clamped_positive(self, memory):
        """Delta > _MAX_TRUST_DELTA is clamped."""
        new_trust = await memory.update_trust("bob", delta=0.5, reason="huge")
        assert new_trust == pytest.approx(
            _DEFAULT_TRUST + _MAX_TRUST_DELTA, abs=0.001,
        )

    async def test_delta_clamped_negative(self, memory):
        """Delta < -_MAX_TRUST_DELTA is clamped."""
        new_trust = await memory.update_trust("bob", delta=-0.8, reason="huge fail")
        assert new_trust == pytest.approx(
            _DEFAULT_TRUST - _MAX_TRUST_DELTA, abs=0.001,
        )

    async def test_trust_clamped_to_max(self, memory):
        """Trust cannot exceed 1.0."""
        # Start from 0.5, add 0.2 three times
        await memory.update_trust("bob", delta=0.2, reason="r1")
        await memory.update_trust("bob", delta=0.2, reason="r2")
        trust = await memory.update_trust("bob", delta=0.2, reason="r3")
        assert trust <= 1.0
        assert trust == pytest.approx(1.0, abs=0.001)

    async def test_trust_clamped_to_min(self, memory):
        """Trust cannot go below 0.0."""
        await memory.update_trust("bob", delta=-0.2, reason="r1")
        await memory.update_trust("bob", delta=-0.2, reason="r2")
        trust = await memory.update_trust("bob", delta=-0.2, reason="r3")
        assert trust >= 0.0
        assert trust == pytest.approx(0.0, abs=0.001)

    async def test_sequential_updates_accumulate(self, memory):
        await memory.update_trust("bob", delta=0.1, reason="r1")
        await memory.update_trust("bob", delta=0.05, reason="r2")
        trust = await memory.get_trust("bob")
        assert trust == pytest.approx(0.65, abs=0.001)

    async def test_reason_stored_as_notes(self, memory):
        await memory.update_trust("bob", delta=0.1, reason="great code review")
        summary = await memory.get_relationship_summary("bob")
        assert summary.notes == "great code review"

    async def test_zero_delta(self, memory):
        new_trust = await memory.update_trust("bob", delta=0.0, reason="no change")
        assert new_trust == pytest.approx(0.5, abs=0.001)

    async def test_concurrent_updates_both_applied(self, memory):
        """Concurrent update_trust() calls must both apply (TOCTOU safety).

        Validates the SQL-level arithmetic documented in update_trust():
        two concurrent +0.1 deltas on default 0.5 should yield 0.7.
        """
        import asyncio

        await asyncio.gather(
            memory.update_trust("bob", 0.1, "r1"),
            memory.update_trust("bob", 0.1, "r2"),
        )
        trust = await memory.get_trust("bob")
        assert trust == pytest.approx(0.7, abs=0.001)

    async def test_delta_rejects_nan_inf(self, memory):
        """NaN and Inf deltas are rejected to prevent SQLite corruption."""
        with pytest.raises(ValueError, match="finite"):
            await memory.update_trust("bob", delta=float("nan"), reason="r")
        with pytest.raises(ValueError, match="finite"):
            await memory.update_trust("bob", delta=float("inf"), reason="r")
        with pytest.raises(ValueError, match="finite"):
            await memory.update_trust("bob", delta=float("-inf"), reason="r")


# ─── Bidirectional decay ────────────────────────────────────


class TestApplyDecay:
    async def test_high_trust_decays_toward_neutral(self, memory):
        await memory.update_trust("bob", delta=0.2, reason="good")
        # trust is now 0.7
        await memory.apply_decay(decay_rate=0.1)
        trust = await memory.get_trust("bob")
        # 0.7 + 0.1 * (0.5 - 0.7) = 0.7 - 0.02 = 0.68
        assert trust == pytest.approx(0.68, abs=0.001)

    async def test_low_trust_decays_toward_neutral(self, memory):
        await memory.update_trust("bob", delta=-0.2, reason="bad")
        # trust is now 0.3
        await memory.apply_decay(decay_rate=0.1)
        trust = await memory.get_trust("bob")
        # 0.3 + 0.1 * (0.5 - 0.3) = 0.3 + 0.02 = 0.32
        assert trust == pytest.approx(0.32, abs=0.001)

    async def test_neutral_trust_unaffected(self, memory):
        """Trust at exactly 0.5 should not change."""
        # Create a relationship at default trust
        await memory.record_interaction("bob", "chat")
        await memory.apply_decay(decay_rate=0.5)
        trust = await memory.get_trust("bob")
        assert trust == pytest.approx(0.5, abs=0.001)

    async def test_decay_multiple_relationships(self, memory):
        await memory.update_trust("bob", delta=0.2, reason="good")
        await memory.update_trust("alice", delta=-0.15, reason="bad")
        updated = await memory.apply_decay(decay_rate=0.1)
        assert updated == 2
        # bob: 0.7 → 0.68, alice: 0.35 → 0.365
        assert await memory.get_trust("bob") == pytest.approx(0.68, abs=0.001)
        assert await memory.get_trust("alice") == pytest.approx(0.365, abs=0.001)

    async def test_decay_returns_count(self, memory):
        updated = await memory.apply_decay()
        assert updated == 0
        await memory.update_trust("bob", delta=0.1, reason="good")
        updated = await memory.apply_decay()
        assert updated == 1

    async def test_invalid_decay_rate(self, memory):
        with pytest.raises(ValueError, match="decay_rate"):
            await memory.apply_decay(decay_rate=0.0)
        with pytest.raises(ValueError, match="decay_rate"):
            await memory.apply_decay(decay_rate=-0.1)
        with pytest.raises(ValueError, match="decay_rate"):
            await memory.apply_decay(decay_rate=1.5)

    async def test_decay_rate_rejects_nan_inf(self, memory):
        """NaN and Inf decay rates are explicitly rejected."""
        with pytest.raises(ValueError, match="decay_rate"):
            await memory.apply_decay(decay_rate=float("nan"))
        with pytest.raises(ValueError, match="decay_rate"):
            await memory.apply_decay(decay_rate=float("inf"))

    async def test_repeated_decay_converges(self, memory):
        """Repeated decay should converge toward 0.5."""
        await memory.update_trust("bob", delta=0.2, reason="good")
        for _ in range(100):
            await memory.apply_decay(decay_rate=0.1)
        trust = await memory.get_trust("bob")
        assert trust == pytest.approx(0.5, abs=0.01)


# ─── Empty other_id validation (R-1 / F-4-1) ───────────────


class TestEmptyOtherAgentId:
    """update_trust() and record_interaction() reject empty other_id."""

    async def test_update_trust_rejects_empty(self, memory):
        with pytest.raises(ValueError, match="other_id must not be empty"):
            await memory.update_trust("", 0.1, "reason")

    async def test_update_trust_rejects_whitespace(self, memory):
        with pytest.raises(ValueError, match="other_id must not be empty"):
            await memory.update_trust("   ", 0.1, "reason")

    async def test_record_interaction_rejects_empty(self, memory):
        with pytest.raises(ValueError, match="other_id must not be empty"):
            await memory.record_interaction("", "chat")

    async def test_record_interaction_rejects_whitespace(self, memory):
        with pytest.raises(ValueError, match="other_id must not be empty"):
            await memory.record_interaction("   ", "chat")


# ─── String truncation (R-2 / F-4-3, F-4-4) ────────────────


class TestStringTruncation:
    """reason and outcome are capped at 1024 chars to bound storage and LLM context."""

    async def test_reason_truncated_at_1024(self, memory):
        long_reason = "x" * 2000
        await memory.update_trust("bob", 0.1, long_reason)
        summary = await memory.get_relationship_summary("bob")
        assert len(summary.notes) == 1024
        assert summary.notes.endswith("..."), "truncated reason should end with '...' indicator"

    async def test_outcome_truncated_at_1024(self, memory):
        long_outcome = "y" * 2000
        await memory.record_interaction("bob", "chat", outcome=long_outcome)
        summary = await memory.get_relationship_summary("bob")
        assert len(summary.recent_interactions[0].outcome) == 1024
        assert summary.recent_interactions[0].outcome.endswith("..."), (
            "truncated outcome should end with '...' indicator"
        )

    async def test_short_reason_unchanged(self, memory):
        reason = "short reason"
        await memory.update_trust("bob", 0.1, reason)
        summary = await memory.get_relationship_summary("bob")
        assert summary.notes == reason

    async def test_none_outcome_unchanged(self, memory):
        await memory.record_interaction("bob", "chat", outcome=None)
        summary = await memory.get_relationship_summary("bob")
        assert summary.recent_interactions[0].outcome is None


class TestSentimentBoundaryValues:
    """Exact boundary values ±1.0 should pass through unclamped (R-08)."""

    async def test_sentiment_exact_positive_boundary(self, memory):
        await memory.record_interaction("bob", "chat", sentiment=1.0)
        summary = await memory.get_relationship_summary("bob")
        assert summary.recent_interactions[0].sentiment == 1.0

    async def test_sentiment_exact_negative_boundary(self, memory):
        await memory.record_interaction("bob", "chat", sentiment=-1.0)
        summary = await memory.get_relationship_summary("bob")
        assert summary.recent_interactions[0].sentiment == -1.0
