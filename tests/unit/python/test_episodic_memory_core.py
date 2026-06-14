"""
Tests for EpisodicMemory — core functionality: data model, schema migrations,
store/recall, access tracking, agent isolation, and edge cases.
"""

import time

import pytest

from agents.memory.episodic import Episode, EpisodicMemory
from agents.memory.migrations import _apply_migrations

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
        async with db.execute(
            "SELECT version, description FROM schema_version ORDER BY version"
        ) as cursor:
            rows = list(await cursor.fetchall())
        # Schema-version row count + per-row identity pin: every new
        # migration MUST bump both the count and add a (version,
        # description-substring) assertion here.  The count bumped from
        # 13 → 14 alongside migration v14 (RFC 0031 amendment — F-7
        # Option D, ISSUE-0093, PR D4: backfill contact notes onto
        # relationship identity).  14 → 15 alongside migration v15
        # (ISSUE-0102 PR 2: governance_interaction_id column on episodes).
        assert len(rows) == 15
        assert rows[0][0] == 1
        assert "Initial schema" in rows[0][1]
        assert rows[1][0] == 2
        assert "Notes" in rows[1][1]
        assert rows[2][0] == 3
        assert "Relationships" in rows[2][1]
        assert rows[3][0] == 4
        assert "participant" in rows[3][1].lower()
        assert rows[4][0] == 5
        assert "interaction" in rows[4][1].lower()
        assert rows[5][0] == 6
        assert "procedural" in rows[5][1].lower()
        assert rows[6][0] == 7
        assert "session_id" in rows[6][1]
        assert rows[7][0] == 8
        assert "facts" in rows[7][1].lower()
        # v9 substring uses ``"session_id on notes"`` rather than the
        # plain ``"session_id"`` substring used at v6→v7 so the pin
        # disambiguates against v7's
        # ``"RFC 0031: session_id on episodes + relationships"`` —
        # both rows share the ``session_id`` token, so a swap would
        # otherwise round-trip green.
        assert rows[8][0] == 9
        assert "session_id on notes" in rows[8][1].lower()
        # v10 disambiguates against v7 / v9 via the ``interactions``
        # token (the table name).
        assert rows[9][0] == 10
        assert "session_id on interactions" in rows[9][1].lower()
        # v11 is the orthogonal tenant axis — disambiguated by the
        # ``principal_id`` token (no other migration carries it).
        assert rows[10][0] == 11
        assert "principal_id" in rows[10][1].lower()
        # v12 is the orthogonal run/test-isolation axis — disambiguated by
        # the ``epoch_id`` token (no other migration carries it).
        assert rows[11][0] == 12
        assert "epoch_id" in rows[11][1].lower()
        # v13 homes person identity on the cross-room relationship tier —
        # disambiguated by the ``identity`` token (no other migration
        # carries it).
        assert rows[12][0] == 13
        assert "identity" in rows[12][1].lower()
        # v14 backfills pre-cutover contact notes onto that identity —
        # disambiguated by the ``backfill`` token (no other migration
        # carries it).
        assert rows[13][0] == 14
        assert "backfill" in rows[13][1].lower()
        # v15 promotes the RFC 0030 governance interaction id to a queryable
        # episodes column — disambiguated by the ``governance`` token (no
        # other migration carries it).
        assert rows[14][0] == 15
        assert "governance" in rows[14][1].lower()

    async def test_migrations_are_idempotent(self, memory: EpisodicMemory):
        """Re-running migrations does not error or duplicate rows."""
        db = memory._ensure_db()
        await _apply_migrations(db)
        async with db.execute("SELECT COUNT(*) FROM schema_version") as cursor:
            row = await cursor.fetchone()
        assert row is not None
        # Bumped from 13 → 14 alongside migration v14 (RFC 0031 amendment —
        # F-7 Option D, ISSUE-0093, PR D4: backfill contact notes onto
        # relationship identity).  14 → 15 alongside migration v15 (ISSUE-0102
        # PR 2: governance_interaction_id column on episodes).
        # Same row-count discipline as ``test_migration_version_recorded``.
        assert row[0] == 15

    async def test_wal_mode_enabled(self):
        """WAL mode is set on file-based databases (not :memory:)."""
        import os
        import tempfile

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
            context={"pr": 42, "repo": "Persatrix"},
            outcome="approved",
            importance=0.9,
            tags=["review", "code"],
        )
        ep = await memory.get_episode(ep_id)
        assert ep is not None
        assert ep.summary == "Reviewed PR #42"
        assert ep.context == {"pr": 42, "repo": "Persatrix"}
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
        await memory.store_episode(
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


# ─── MemoryLifecycle protocol (EpisodicMemory) ─────────────


class TestEpisodicMemoryLifecycle:
    """Verify EpisodicMemory satisfies MemoryLifecycle protocol (PR #59 review).

    WorkingMemory was already tested in test_working_memory.py (F-2-5).
    EpisodicMemory structurally satisfies the same protocol — both have
    async initialize() and async close() with matching signatures.
    """

    def test_episodic_memory_satisfies_memory_lifecycle(self):
        from agents.memory import MemoryLifecycle
        assert isinstance(
            EpisodicMemory(agent_id="test", db_path=":memory:"),
            MemoryLifecycle,
        )
