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
            agent_id="a",
            other_agent_id="b",
            interaction_type="task_delegation",
            outcome="success",
            sentiment=0.8,
            created_at=1000.0,
        )
        assert i.interaction_type == "task_delegation"
        assert i.sentiment == 0.8

    def test_relationship_summary_defaults(self):
        s = RelationshipSummary(
            other_agent_id="b",
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


# ─── Interaction recording ──────────────────────────────────


class TestRecordInteraction:
    async def test_basic_record(self, memory):
        iid = await memory.record_interaction(
            "bob", "task_delegation", outcome="success", sentiment=0.8,
        )
        assert iid  # non-empty UUID

    async def test_increments_interaction_count(self, memory):
        await memory.record_interaction("bob", "chat")
        await memory.record_interaction("bob", "review")
        summary = await memory.get_relationship_summary("bob")
        assert summary.interaction_count == 2

    async def test_creates_relationship_if_missing(self, memory):
        """First interaction creates the relationship row at default trust."""
        await memory.record_interaction("bob", "chat")
        trust = await memory.get_trust("bob")
        assert trust == pytest.approx(_DEFAULT_TRUST, abs=0.001)

    async def test_records_with_defaults(self, memory):
        await memory.record_interaction("bob", "chat")
        summary = await memory.get_relationship_summary("bob")
        assert len(summary.recent_interactions) == 1
        assert summary.recent_interactions[0].outcome is None
        assert summary.recent_interactions[0].sentiment == 0.0

    async def test_updates_last_interaction_at(self, memory):
        await memory.record_interaction("bob", "chat")
        summary = await memory.get_relationship_summary("bob")
        assert summary.last_interaction_at is not None

    async def test_sentiment_clamped_to_bounds(self, memory):
        """Sentiment values outside [-1.0, 1.0] are clamped."""
        await memory.record_interaction("bob", "chat", sentiment=5.0)
        summary = await memory.get_relationship_summary("bob")
        assert summary.recent_interactions[0].sentiment == 1.0

        await memory.record_interaction("bob", "chat", sentiment=-9.9)
        # Newest first — index 0 is the -9.9 → -1.0 entry.
        summary = await memory.get_relationship_summary("bob")
        assert summary.recent_interactions[0].sentiment == -1.0

    async def test_sentiment_rejects_nan_inf(self, memory):
        """NaN and Inf are rejected to prevent corrupt aggregation."""
        with pytest.raises(ValueError, match="finite"):
            await memory.record_interaction("bob", "chat", sentiment=float("nan"))
        with pytest.raises(ValueError, match="finite"):
            await memory.record_interaction("bob", "chat", sentiment=float("inf"))
        with pytest.raises(ValueError, match="finite"):
            await memory.record_interaction("bob", "chat", sentiment=float("-inf"))

    async def test_empty_interaction_type_rejected(self, memory):
        """Empty or whitespace-only interaction_type is rejected (F-10)."""
        with pytest.raises(ValueError, match="interaction_type"):
            await memory.record_interaction("bob", "")
        with pytest.raises(ValueError, match="interaction_type"):
            await memory.record_interaction("bob", "   ")


# ─── Relationship summary ───────────────────────────────────


class TestGetRelationshipSummary:
    async def test_unknown_agent_returns_defaults(self, memory):
        summary = await memory.get_relationship_summary("unknown")
        assert summary.other_agent_id == "unknown"
        assert summary.trust_score == _DEFAULT_TRUST
        assert summary.interaction_count == 0
        assert summary.last_interaction_at is None
        assert summary.notes is None
        assert summary.recent_interactions == []

    async def test_includes_trust_and_interactions(self, memory):
        await memory.update_trust("bob", delta=0.1, reason="good")
        await memory.record_interaction("bob", "code_review", outcome="approved")
        summary = await memory.get_relationship_summary("bob")
        assert summary.trust_score == pytest.approx(0.6, abs=0.001)
        assert summary.interaction_count == 1
        assert len(summary.recent_interactions) == 1
        assert summary.recent_interactions[0].interaction_type == "code_review"

    async def test_recent_interactions_ordered_newest_first(self, memory):
        await memory.record_interaction("bob", "first")
        await memory.record_interaction("bob", "second")
        await memory.record_interaction("bob", "third")
        summary = await memory.get_relationship_summary("bob")
        types = [i.interaction_type for i in summary.recent_interactions]
        assert types == ["third", "second", "first"]

    async def test_limits_to_max_recent_interactions(self, memory):
        """Only the most recent _MAX_RECENT_INTERACTIONS are returned."""
        total = _MAX_RECENT_INTERACTIONS + 5
        for i in range(total):
            await memory.record_interaction("bob", f"chat-{i}")
        summary = await memory.get_relationship_summary("bob")
        assert len(summary.recent_interactions) == _MAX_RECENT_INTERACTIONS
        # Newest first: last recorded interaction is first in the list.
        assert summary.recent_interactions[0].interaction_type == f"chat-{total - 1}"
        assert summary.recent_interactions[-1].interaction_type == f"chat-{total - _MAX_RECENT_INTERACTIONS}"

    async def test_trust_only_relationship_has_null_last_interaction(self, memory):
        """update_trust() alone should NOT set last_interaction_at."""
        await memory.update_trust("bob", delta=0.1, reason="good work")
        summary = await memory.get_relationship_summary("bob")
        assert summary.last_interaction_at is None

    async def test_trust_preserved_after_interaction(self, memory):
        """record_interaction() UPSERT preserves trust set by update_trust()."""
        await memory.update_trust("bob", delta=0.1, reason="good work")
        await memory.record_interaction("bob", "code_review")
        summary = await memory.get_relationship_summary("bob")
        assert summary.trust_score == pytest.approx(0.6, abs=0.001)
        assert summary.interaction_count == 1
        assert summary.notes == "good work"

    async def test_summary_reflects_decayed_trust(self, memory):
        """get_relationship_summary() returns trust after apply_decay() (F-17)."""
        await memory.update_trust("bob", delta=0.2, reason="good")
        await memory.apply_decay(decay_rate=0.1)
        summary = await memory.get_relationship_summary("bob")
        # 0.7 + 0.1 * (0.5 - 0.7) = 0.68
        assert summary.trust_score == pytest.approx(0.68, abs=0.001)


class TestGetAllRelationships:
    async def test_empty_when_no_relationships(self, memory):
        rels = await memory.get_all_relationships()
        assert rels == []

    async def test_returns_all_relationships(self, memory):
        await memory.update_trust("bob", delta=0.1, reason="good")
        await memory.update_trust("alice", delta=-0.1, reason="bad")
        rels = await memory.get_all_relationships()
        assert len(rels) == 2
        # Ordered by trust_score DESC
        assert rels[0].other_agent_id == "bob"
        assert rels[1].other_agent_id == "alice"

    async def test_recent_interactions_not_populated(self, memory):
        """get_all_relationships() does not populate recent_interactions."""
        await memory.update_trust("bob", delta=0.1, reason="good")
        await memory.record_interaction("bob", "chat")
        rels = await memory.get_all_relationships()
        assert rels[0].recent_interactions == []


# ─── Trust bootstrapping from config ────────────────────────


class TestTrustBootstrapping:
    async def test_seeds_from_config(self):
        mem = RelationshipMemory(agent_id="sarah", db_path=":memory:")
        config = [
            {"agent_id": "mike", "trust_level": 0.9},
            {"agent_id": "alice", "trust_level": 0.3},
        ]
        await mem.initialize(config_relationships=config)
        try:
            assert await mem.get_trust("mike") == pytest.approx(0.9, abs=0.001)
            assert await mem.get_trust("alice") == pytest.approx(0.3, abs=0.001)
        finally:
            await mem.close()

    async def test_does_not_overwrite_existing(self):
        """Config seeding skips rows that already exist."""
        import os
        import tempfile

        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            # First init: seed mike at 0.9
            mem1 = RelationshipMemory(agent_id="sarah", db_path=path)
            await mem1.initialize(config_relationships=[
                {"agent_id": "mike", "trust_level": 0.9},
            ])
            # Runtime evolves trust to 0.7
            await mem1.update_trust("mike", delta=-0.2, reason="conflict")
            assert await mem1.get_trust("mike") == pytest.approx(0.7, abs=0.001)
            await mem1.close()

            # Second init: config still says 0.9, but runtime 0.7 preserved
            mem2 = RelationshipMemory(agent_id="sarah", db_path=path)
            await mem2.initialize(config_relationships=[
                {"agent_id": "mike", "trust_level": 0.9},
            ])
            assert await mem2.get_trust("mike") == pytest.approx(0.7, abs=0.001)
            await mem2.close()
        finally:
            os.unlink(path)

    async def test_skips_entries_without_trust_level(self):
        mem = RelationshipMemory(agent_id="sarah", db_path=":memory:")
        config = [
            {"agent_id": "mike"},  # no trust_level
            {"trust_level": 0.8},  # no agent_id
        ]
        await mem.initialize(config_relationships=config)
        try:
            # Neither entry created a relationship
            rels = await mem.get_all_relationships()
            assert len(rels) == 0
        finally:
            await mem.close()

    async def test_clamps_config_trust_level(self):
        mem = RelationshipMemory(agent_id="sarah", db_path=":memory:")
        config = [
            {"agent_id": "mike", "trust_level": 1.5},
            {"agent_id": "alice", "trust_level": -0.3},
        ]
        await mem.initialize(config_relationships=config)
        try:
            assert await mem.get_trust("mike") == pytest.approx(1.0, abs=0.001)
            assert await mem.get_trust("alice") == pytest.approx(0.0, abs=0.001)
        finally:
            await mem.close()

    async def test_skips_non_numeric_trust_level(self):
        """Non-numeric trust_level (e.g., 'high') is skipped, not crashed."""
        mem = RelationshipMemory(agent_id="sarah", db_path=":memory:")
        config = [
            {"agent_id": "mike", "trust_level": "high"},
            {"agent_id": "alice", "trust_level": 0.8},  # valid entry still works
        ]
        await mem.initialize(config_relationships=config)
        try:
            # mike was skipped, alice was seeded
            assert await mem.get_trust("mike") == _DEFAULT_TRUST
            assert await mem.get_trust("alice") == pytest.approx(0.8, abs=0.001)
        finally:
            await mem.close()


# ─── Agent isolation ─────────────────────────────────────────


class TestAgentIsolation:
    async def test_trust_scores_are_agent_scoped(self, memory_pair):
        mem_a, mem_b = memory_pair
        await mem_a.update_trust("target", delta=0.2, reason="good")
        await mem_b.update_trust("target", delta=-0.1, reason="bad")

        assert await mem_a.get_trust("target") == pytest.approx(0.7, abs=0.001)
        assert await mem_b.get_trust("target") == pytest.approx(0.4, abs=0.001)

    async def test_interactions_are_agent_scoped(self, memory_pair):
        mem_a, mem_b = memory_pair
        await mem_a.record_interaction("target", "chat")
        await mem_a.record_interaction("target", "review")
        await mem_b.record_interaction("target", "planning")

        summary_a = await mem_a.get_relationship_summary("target")
        summary_b = await mem_b.get_relationship_summary("target")
        assert summary_a.interaction_count == 2
        assert summary_b.interaction_count == 1

    async def test_decay_is_agent_scoped(self, memory_pair):
        mem_a, mem_b = memory_pair
        await mem_a.update_trust("target", delta=0.2, reason="good")
        await mem_b.update_trust("target", delta=0.2, reason="good")

        await mem_a.apply_decay(decay_rate=0.5)

        # Agent A's trust decayed, agent B's unchanged
        assert await mem_a.get_trust("target") == pytest.approx(0.6, abs=0.001)
        assert await mem_b.get_trust("target") == pytest.approx(0.7, abs=0.001)

    async def test_all_relationships_agent_scoped(self, memory_pair):
        mem_a, mem_b = memory_pair
        await mem_a.update_trust("bob", delta=0.1, reason="good")
        await mem_b.update_trust("charlie", delta=0.1, reason="good")

        rels_a = await mem_a.get_all_relationships()
        rels_b = await mem_b.get_all_relationships()
        assert len(rels_a) == 1
        assert rels_a[0].other_agent_id == "bob"
        assert len(rels_b) == 1
        assert rels_b[0].other_agent_id == "charlie"


# ─── Concurrent record_interaction (F-4-2) ─────────────────


class TestConcurrentRecordInteraction:
    async def test_concurrent_record_interactions_both_counted(self, memory: RelationshipMemory):
        """Two concurrent record_interaction() calls should both be counted."""
        import asyncio

        await asyncio.gather(
            memory.record_interaction("bob", "chat", outcome="good"),
            memory.record_interaction("bob", "review", outcome="great"),
        )
        summary = await memory.get_relationship_summary("bob")
        assert summary.interaction_count == 2
