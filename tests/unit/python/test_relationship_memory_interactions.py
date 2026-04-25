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
        assert summary.other_participant_id == "unknown"
        assert summary.other_participant_type == "agent"
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
        assert rels[0].other_participant_id == "bob"
        assert rels[1].other_participant_id == "alice"

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
        assert rels_a[0].other_participant_id == "bob"
        assert len(rels_b) == 1
        assert rels_b[0].other_participant_id == "charlie"


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
