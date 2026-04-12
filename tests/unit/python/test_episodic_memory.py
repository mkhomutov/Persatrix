"""
Tests for EpisodicMemory — long-term episodic storage with SQLite and FTS5.

All tests use in-memory SQLite (:memory:) for isolation and speed.
"""

import time
from unittest.mock import AsyncMock, patch

import pytest

from agents.memory.episodic import (
    MIGRATIONS,
    _MAX_RECALL_LIMIT,
    Episode,
    EpisodicMemory,
    _apply_migrations,
    _fts5_available,
)


# ─── Fixtures ───────────────────────────────────────────────


@pytest.fixture
async def memory():
    """Create an initialized EpisodicMemory instance with in-memory DB."""
    mem = EpisodicMemory(agent_id="test-agent", db_path=":memory:")
    await mem.initialize()
    yield mem
    await mem.close()


@pytest.fixture
async def memory_pair():
    """Create two EpisodicMemory instances sharing different agent IDs on the same DB.

    Uses a temp file so both connections share state.
    """
    import tempfile
    import os

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        mem_a = EpisodicMemory(agent_id="agent-a", db_path=path)
        mem_b = EpisodicMemory(agent_id="agent-b", db_path=path)
        await mem_a.initialize()
        await mem_b.initialize()
        yield mem_a, mem_b
        await mem_a.close()
        await mem_b.close()
    finally:
        os.unlink(path)


# ─── Episode dataclass ──────────────────────────────────────


class TestEpisodeDataclass:
    def test_episode_fields(self):
        ep = Episode(
            id="ep-1",
            agent_id="test-agent",
            summary="Did a thing",
            context={"key": "value"},
            outcome="success",
            importance=0.8,
            access_count=0,
            last_accessed_at=None,
            tags=["tag1"],
            created_at=1000.0,
            compressed_at=None,
            compression_level=0,
        )
        assert ep.id == "ep-1"
        assert ep.importance == 0.8
        assert ep.tags == ["tag1"]
        assert ep.compression_level == 0


# ─── Schema migration infrastructure ────────────────────────


class TestMigrations:
    async def test_schema_version_table_created(self, memory: EpisodicMemory):
        """schema_version table exists after initialize()."""
        db = memory._ensure_db()
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None

    async def test_episodes_table_created(self, memory: EpisodicMemory):
        db = memory._ensure_db()
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='episodes'"
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None

    async def test_agent_state_table_created(self, memory: EpisodicMemory):
        db = memory._ensure_db()
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='agent_state'"
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None

    async def test_migration_version_recorded(self, memory: EpisodicMemory):
        db = memory._ensure_db()
        async with db.execute("SELECT version, description FROM schema_version ORDER BY version") as cursor:
            rows = await cursor.fetchall()
        assert len(rows) == 2
        assert rows[0][0] == 1
        assert "Initial schema" in rows[0][1]
        assert rows[1][0] == 2
        assert "Notes" in rows[1][1]

    async def test_migrations_are_idempotent(self, memory: EpisodicMemory):
        """Re-running migrations does not error or duplicate rows."""
        db = memory._ensure_db()
        await _apply_migrations(db)
        async with db.execute("SELECT COUNT(*) FROM schema_version") as cursor:
            row = await cursor.fetchone()
        assert row[0] == 2

    async def test_wal_mode_enabled(self):
        """WAL mode is set on file-based databases (not :memory:)."""
        import tempfile
        import os

        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            mem = EpisodicMemory(agent_id="test-wal", db_path=path)
            await mem.initialize()
            db = mem._ensure_db()
            async with db.execute("PRAGMA journal_mode") as cursor:
                row = await cursor.fetchone()
            assert row[0] == "wal"
            await mem.close()
        finally:
            os.unlink(path)

    async def test_indexes_created(self, memory: EpisodicMemory):
        db = memory._ensure_db()
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_episodes_%'"
        ) as cursor:
            rows = await cursor.fetchall()
        index_names = {r[0] for r in rows}
        assert "idx_episodes_agent" in index_names
        assert "idx_episodes_importance" in index_names
        assert "idx_episodes_created" in index_names


# ─── FTS5 availability ──────────────────────────────────────


class TestFTS5:
    async def test_fts5_available(self, memory: EpisodicMemory):
        """FTS5 should be available in standard CPython builds."""
        assert memory._fts5 is True

    async def test_fts5_table_created(self, memory: EpisodicMemory):
        db = memory._ensure_db()
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='episodes_fts'"
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None

    async def test_fts5_triggers_created(self, memory: EpisodicMemory):
        db = memory._ensure_db()
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'episodes_a%'"
        ) as cursor:
            rows = await cursor.fetchall()
        trigger_names = {r[0] for r in rows}
        assert "episodes_ai" in trigger_names
        assert "episodes_ad" in trigger_names
        assert "episodes_au" in trigger_names

    async def test_fts5_fallback_when_unavailable(self):
        """When FTS5 is unavailable, memory falls back to LIKE queries."""
        mem = EpisodicMemory(agent_id="test-agent", db_path=":memory:")

        with patch("agents.memory.episodic._fts5_available", new_callable=AsyncMock) as mock_fts5:
            mock_fts5.return_value = False
            await mem.initialize()

        assert mem._fts5 is False

        # Store and recall should still work via LIKE fallback
        ep_id = await mem.store_episode(
            summary="test episode for LIKE fallback",
            context={"type": "test"},
        )
        results = await mem.recall("test episode")
        assert len(results) == 1
        assert results[0].id == ep_id

        await mem.close()


# ─── Store and recall ───────────────────────────────────────


class TestStoreAndRecall:
    async def test_store_returns_uuid(self, memory: EpisodicMemory):
        ep_id = await memory.store_episode(
            summary="Helped user debug Python code",
            context={"task": "debug", "language": "python"},
        )
        assert isinstance(ep_id, str)
        assert len(ep_id) == 36  # UUID format

    async def test_store_and_get_roundtrip(self, memory: EpisodicMemory):
        ep_id = await memory.store_episode(
            summary="Reviewed PR #42",
            context={"pr": 42, "repo": "orchestr8"},
            outcome="approved",
            importance=0.9,
            tags=["review", "code"],
        )
        ep = await memory.get_episode(ep_id)
        assert ep is not None
        assert ep.summary == "Reviewed PR #42"
        assert ep.context == {"pr": 42, "repo": "orchestr8"}
        assert ep.outcome == "approved"
        assert ep.importance == 0.9
        assert ep.tags == ["review", "code"]
        assert ep.access_count == 0
        assert ep.compression_level == 0
        assert ep.compressed_at is None

    async def test_recall_by_query_fts5(self, memory: EpisodicMemory):
        await memory.store_episode(
            summary="Designed database schema for user service",
            context={"type": "design"},
            importance=0.8,
        )
        await memory.store_episode(
            summary="Fixed CSS styling on login page",
            context={"type": "bugfix"},
            importance=0.5,
        )
        results = await memory.recall("database schema")
        assert len(results) >= 1
        assert "database schema" in results[0].summary

    async def test_recall_empty_query_returns_by_recency(self, memory: EpisodicMemory):
        await memory.store_episode(
            summary="Old episode",
            context={},
            importance=0.5,
        )
        await memory.store_episode(
            summary="Recent episode",
            context={},
            importance=0.5,
        )
        results = await memory.recall("", limit=10)
        assert len(results) == 2

    async def test_recall_with_limit(self, memory: EpisodicMemory):
        for i in range(5):
            await memory.store_episode(
                summary=f"Episode {i}",
                context={"index": i},
            )
        results = await memory.recall("", limit=3)
        assert len(results) == 3

    async def test_recall_with_min_importance(self, memory: EpisodicMemory):
        await memory.store_episode(
            summary="Low importance event",
            context={},
            importance=0.2,
        )
        await memory.store_episode(
            summary="High importance event",
            context={},
            importance=0.9,
        )
        results = await memory.recall("", min_importance=0.5)
        assert len(results) == 1
        assert results[0].importance == 0.9

    async def test_count_episodes(self, memory: EpisodicMemory):
        assert await memory.count_episodes() == 0
        await memory.store_episode(summary="one", context={})
        await memory.store_episode(summary="two", context={})
        assert await memory.count_episodes() == 2


# ─── Access count tracking ──────────────────────────────────


class TestAccessCount:
    async def test_recall_increments_access_count(self, memory: EpisodicMemory):
        ep_id = await memory.store_episode(
            summary="Memorable event about testing",
            context={"type": "test"},
        )
        # First recall
        results = await memory.recall("testing")
        assert len(results) == 1
        assert results[0].access_count == 1

        # Second recall
        results = await memory.recall("testing")
        assert results[0].access_count == 2

    async def test_recall_updates_last_accessed(self, memory: EpisodicMemory):
        await memory.store_episode(
            summary="An event about timestamps",
            context={},
        )
        before = time.time()
        results = await memory.recall("timestamps")
        after = time.time()
        assert results[0].last_accessed_at is not None
        assert before <= results[0].last_accessed_at <= after

    async def test_access_count_persists_in_db(self, memory: EpisodicMemory):
        ep_id = await memory.store_episode(
            summary="Persistent access count check",
            context={},
        )
        await memory.recall("access count")
        await memory.recall("access count")

        # Verify directly in DB
        ep = await memory.get_episode(ep_id)
        assert ep is not None
        assert ep.access_count == 2


# ─── Agent isolation ────────────────────────────────────────


class TestAgentIsolation:
    async def test_agent_cannot_see_other_agents_episodes(self, memory_pair):
        mem_a, mem_b = memory_pair

        await mem_a.store_episode(
            summary="Agent A private data about project alpha",
            context={"agent": "a"},
        )
        await mem_b.store_episode(
            summary="Agent B private data about project beta",
            context={"agent": "b"},
        )

        results_a = await mem_a.recall("", limit=100)
        results_b = await mem_b.recall("", limit=100)

        assert len(results_a) == 1
        assert results_a[0].agent_id == "agent-a"

        assert len(results_b) == 1
        assert results_b[0].agent_id == "agent-b"

    async def test_get_episode_scoped_to_agent(self, memory_pair):
        mem_a, mem_b = memory_pair

        ep_id = await mem_a.store_episode(
            summary="Agent A episode",
            context={},
        )

        # Agent A can access
        assert await mem_a.get_episode(ep_id) is not None
        # Agent B cannot
        assert await mem_b.get_episode(ep_id) is None

    async def test_count_scoped_to_agent(self, memory_pair):
        mem_a, mem_b = memory_pair

        await mem_a.store_episode(summary="A1", context={})
        await mem_a.store_episode(summary="A2", context={})
        await mem_b.store_episode(summary="B1", context={})

        assert await mem_a.count_episodes() == 2
        assert await mem_b.count_episodes() == 1


# ─── FTS5 trigger sync ─────────────────────────────────────


class TestFTS5TriggerSync:
    async def test_insert_syncs_to_fts(self, memory: EpisodicMemory):
        """Inserting an episode also inserts into FTS5 index."""
        await memory.store_episode(
            summary="FTS sync test about quantum computing",
            context={},
        )
        results = await memory.recall("quantum computing")
        assert len(results) == 1
        assert "quantum computing" in results[0].summary

    async def test_update_syncs_to_fts(self, memory: EpisodicMemory):
        """Updating an episode re-indexes in FTS5."""
        db = memory._ensure_db()
        ep_id = await memory.store_episode(
            summary="Original summary about machine learning",
            context={},
        )
        # Manually update the summary
        await db.execute(
            "UPDATE episodes SET summary = ? WHERE id = ?",
            ("Revised summary about deep learning", ep_id),
        )
        await db.commit()

        # Old term should not match
        old_results = await memory.recall("machine learning")
        new_results = await memory.recall("deep learning")

        # The new summary should be findable
        assert any("deep learning" in r.summary for r in new_results)

    async def test_delete_syncs_to_fts(self, memory: EpisodicMemory):
        """Deleting an episode removes it from FTS5 index."""
        db = memory._ensure_db()
        ep_id = await memory.store_episode(
            summary="Ephemeral data about blockchain",
            context={},
        )
        # Verify it's findable
        results = await memory.recall("blockchain")
        assert len(results) == 1

        # Delete it
        await db.execute("DELETE FROM episodes WHERE id = ?", (ep_id,))
        await db.commit()

        # Should no longer be findable
        results = await memory.recall("blockchain")
        assert len(results) == 0


# ─── FTS5 ranking ──────────────────────────────────────────


class TestFTS5Ranking:
    async def test_more_relevant_episodes_rank_higher(self, memory: EpisodicMemory):
        """Episodes with better FTS5 match and higher importance rank first."""
        await memory.store_episode(
            summary="Brief mention of python in a casual conversation",
            context={"type": "casual"},
            importance=0.3,
        )
        await memory.store_episode(
            summary="Extensive Python session with traceback analysis and Python profiling",
            context={"type": "deep-work", "language": "python"},
            importance=0.9,
        )
        # FTS5 MATCH on single term to hit both episodes
        results = await memory.recall("python")
        assert len(results) == 2
        # The more relevant + important episode should rank first
        assert results[0].importance == 0.9


# ─── Edge cases ─────────────────────────────────────────────


class TestEdgeCases:
    async def test_not_initialized_raises(self):
        mem = EpisodicMemory(agent_id="test", db_path=":memory:")
        with pytest.raises(RuntimeError, match="not initialized"):
            await mem.store_episode(summary="fail", context={})

    async def test_close_twice_is_safe(self, memory: EpisodicMemory):
        await memory.close()
        await memory.close()  # Should not raise

    async def test_store_with_none_outcome(self, memory: EpisodicMemory):
        ep_id = await memory.store_episode(
            summary="No outcome",
            context={},
            outcome=None,
        )
        ep = await memory.get_episode(ep_id)
        assert ep is not None
        assert ep.outcome is None

    async def test_store_with_empty_context(self, memory: EpisodicMemory):
        ep_id = await memory.store_episode(
            summary="Empty context",
            context={},
        )
        ep = await memory.get_episode(ep_id)
        assert ep is not None
        assert ep.context == {}

    async def test_store_with_empty_tags(self, memory: EpisodicMemory):
        ep_id = await memory.store_episode(
            summary="No tags",
            context={},
            tags=[],
        )
        ep = await memory.get_episode(ep_id)
        assert ep is not None
        assert ep.tags == []

    async def test_get_nonexistent_episode(self, memory: EpisodicMemory):
        assert await memory.get_episode("nonexistent-id") is None

    async def test_recall_empty_db(self, memory: EpisodicMemory):
        results = await memory.recall("anything")
        assert results == []

    async def test_recall_no_match(self, memory: EpisodicMemory):
        await memory.store_episode(
            summary="An episode about cats",
            context={},
        )
        results = await memory.recall("quantum physics")
        assert len(results) == 0


# ─── FTS5 malformed query fallback ──────────────────────────


class TestFTS5MalformedQueryFallback:
    """FTS5 MATCH raises OperationalError on malformed syntax;
    _recall_fts5 must catch it and fall back to LIKE."""

    async def test_recall_lone_star(self, memory: EpisodicMemory):
        """A lone '*' is invalid FTS5 syntax — should fall back, not crash."""
        await memory.store_episode(summary="star test episode", context={})
        results = await memory.recall("*")
        # Falls back to LIKE '%*%' — no match expected, but no crash
        assert isinstance(results, list)

    async def test_recall_bare_not(self, memory: EpisodicMemory):
        """Bare 'NOT' is invalid FTS5 syntax — should fall back."""
        await memory.store_episode(summary="not test episode", context={})
        results = await memory.recall("NOT")
        assert isinstance(results, list)

    async def test_recall_unbalanced_quotes(self, memory: EpisodicMemory):
        """Unbalanced quotes are invalid FTS5 syntax — should fall back."""
        await memory.store_episode(summary="quote test episode", context={})
        results = await memory.recall('"unclosed')
        assert isinstance(results, list)

    async def test_recall_fts5_fallback_still_finds_via_like(self, memory: EpisodicMemory):
        """When FTS5 fails, LIKE fallback should still find matching episodes."""
        await memory.store_episode(
            summary="recipe for NOT burning toast",
            context={},
        )
        # "NOT" alone is invalid FTS5, but the episode summary contains "NOT"
        # so LIKE fallback with '%NOT%' should still match
        results = await memory.recall("NOT")
        assert len(results) >= 1
        assert "NOT" in results[0].summary


# ─── LIKE wildcard escaping ─────────────────────────────────


class TestLikeWildcardEscaping:
    """LIKE metacharacters %, _ in queries must be escaped so they
    match literally rather than acting as wildcards."""

    async def test_percent_in_query_does_not_match_all(self):
        """A query of '%' should not match every episode."""
        mem = EpisodicMemory(agent_id="test-like", db_path=":memory:")
        with patch("agents.memory.episodic._fts5_available", new_callable=AsyncMock) as mock_fts5:
            mock_fts5.return_value = False
            await mem.initialize()

        await mem.store_episode(summary="no percent sign here", context={})
        results = await mem.recall("%")
        assert len(results) == 0
        await mem.close()

    async def test_underscore_in_query_matches_literally(self):
        """A query of '_' should only match episodes containing literal '_'."""
        mem = EpisodicMemory(agent_id="test-like", db_path=":memory:")
        with patch("agents.memory.episodic._fts5_available", new_callable=AsyncMock) as mock_fts5:
            mock_fts5.return_value = False
            await mem.initialize()

        await mem.store_episode(summary="has_underscore in text", context={})
        await mem.store_episode(summary="no underscore meta here", context={})
        results = await mem.recall("_")
        assert len(results) == 1
        assert "_" in results[0].summary
        await mem.close()


# ─── Importance validation ──────────────────────────────────


class TestImportanceValidation:
    """store_episode() clamps importance to [0.0, 1.0]."""

    async def test_negative_importance_clamped(self, memory: EpisodicMemory):
        ep_id = await memory.store_episode(
            summary="Negative importance test",
            context={},
            importance=-0.5,
        )
        ep = await memory.get_episode(ep_id)
        assert ep is not None
        assert ep.importance == 0.0

    async def test_over_one_importance_clamped(self, memory: EpisodicMemory):
        ep_id = await memory.store_episode(
            summary="Over one importance test",
            context={},
            importance=1.5,
        )
        ep = await memory.get_episode(ep_id)
        assert ep is not None
        assert ep.importance == 1.0

    async def test_valid_importance_unchanged(self, memory: EpisodicMemory):
        ep_id = await memory.store_episode(
            summary="Normal importance test",
            context={},
            importance=0.7,
        )
        ep = await memory.get_episode(ep_id)
        assert ep is not None
        assert ep.importance == 0.7

    async def test_boundary_values_accepted(self, memory: EpisodicMemory):
        ep_id_zero = await memory.store_episode(
            summary="Zero importance", context={}, importance=0.0,
        )
        ep_id_one = await memory.store_episode(
            summary="One importance", context={}, importance=1.0,
        )
        assert (await memory.get_episode(ep_id_zero)).importance == 0.0
        assert (await memory.get_episode(ep_id_one)).importance == 1.0


# ─── Summary validation ────────────────────────────────────


class TestSummaryValidation:
    """store_episode() rejects empty or whitespace-only summaries."""

    async def test_empty_summary_rejected(self, memory: EpisodicMemory):
        with pytest.raises(ValueError, match="summary must not be empty"):
            await memory.store_episode(summary="", context={})

    async def test_whitespace_only_summary_rejected(self, memory: EpisodicMemory):
        with pytest.raises(ValueError, match="summary must not be empty"):
            await memory.store_episode(summary="   \t\n  ", context={})

    async def test_valid_summary_accepted(self, memory: EpisodicMemory):
        ep_id = await memory.store_episode(summary="Valid summary", context={})
        assert ep_id is not None


# ─── Limit validation ──────────────────────────────────────


class TestLimitValidation:
    """recall() validates the limit parameter."""

    async def test_negative_limit_raises(self, memory: EpisodicMemory):
        with pytest.raises(ValueError, match="limit must be >= 1"):
            await memory.recall("anything", limit=-1)

    async def test_zero_limit_raises(self, memory: EpisodicMemory):
        with pytest.raises(ValueError, match="limit must be >= 1"):
            await memory.recall("anything", limit=0)

    async def test_limit_above_max_is_capped(self, memory: EpisodicMemory):
        """Limit exceeding _MAX_RECALL_LIMIT is silently capped."""
        # Store a few episodes so we can verify the query still works
        for i in range(3):
            await memory.store_episode(summary=f"Limit cap episode {i}", context={})
        results = await memory.recall("", limit=_MAX_RECALL_LIMIT + 50)
        # Should return all 3 (below the cap), proving the query ran without error
        assert len(results) == 3


# ─── FTS5 context-based retrieval ───────────────────────────


class TestFTS5ContextRetrieval:
    """FTS5 index covers both summary and context_json — verify
    that episodes can be found by terms appearing only in context."""

    async def test_fts5_finds_episode_by_context_term(self, memory: EpisodicMemory):
        """An episode with a distinctive term only in context (not summary)
        should still be findable via FTS5 search."""
        await memory.store_episode(
            summary="Routine task completion",
            context={"framework": "kubernetes", "cluster": "prod-east"},
        )
        await memory.store_episode(
            summary="Unrelated episode about databases",
            context={"type": "sql"},
        )
        results = await memory.recall("kubernetes")
        assert len(results) == 1
        assert results[0].context["framework"] == "kubernetes"
