"""
Tests for the RFC 0031 Phase 1 write-path ``session_id`` kwarg.

``EpisodicMemory.store_episode`` and ``RelationshipMemory.record_interaction``
accept ``session_id`` as a keyword-only argument and persist it on the
appropriate row.  The default (``"legacy"``) matches the orchestrator-side
synthetic carve-out so pre-RFC callers produce queryable rows without
ambiguity.

Phase 1 ships **write-path only**; recall-side filtering lands in Phase 2.
These tests assert the write contract — round-trip via direct SQLite read
— without making any recall claims.
"""

from __future__ import annotations

import pytest

from agents.memory.episodic import EpisodicMemory
from agents.memory.relationship import RelationshipMemory


# ─── EpisodicMemory.store_episode ───────────────────────────


class TestStoreEpisodeSessionID:
    async def test_default_writes_legacy(self, memory: EpisodicMemory):
        ep_id = await memory.store_episode("hello", {})
        async with memory._ensure_db().execute(
            "SELECT session_id FROM episodes WHERE id = ?",
            (ep_id,),
        ) as cursor:
            row = await cursor.fetchone()
        assert row[0] == "legacy"

    async def test_explicit_session_id_round_trip(
        self, memory: EpisodicMemory,
    ):
        ep_id = await memory.store_episode(
            "hello", {}, session_id="run-a",
        )
        async with memory._ensure_db().execute(
            "SELECT session_id FROM episodes WHERE id = ?",
            (ep_id,),
        ) as cursor:
            row = await cursor.fetchone()
        assert row[0] == "run-a"

    async def test_two_sessions_coexist_at_storage_layer(
        self, memory: EpisodicMemory,
    ):
        a = await memory.store_episode("a", {}, session_id="run-a")
        b = await memory.store_episode("b", {}, session_id="run-b")
        async with memory._ensure_db().execute(
            "SELECT id, session_id FROM episodes WHERE id IN (?, ?) "
            "ORDER BY id",
            (a, b),
        ) as cursor:
            rows = await cursor.fetchall()
        by_id = {r[0]: r[1] for r in rows}
        assert by_id[a] == "run-a"
        assert by_id[b] == "run-b"


# ─── RelationshipMemory.record_interaction ──────────────────


@pytest.fixture
async def rel_memory():
    mem = RelationshipMemory(agent_id="test-agent", db_path=":memory:")
    await mem.initialize()
    yield mem
    await mem.close()


class TestRecordInteractionSessionID:
    async def test_default_writes_legacy(self, rel_memory: RelationshipMemory):
        iid = await rel_memory.record_interaction("bob", "chat")
        async with rel_memory._ensure_db().execute(
            "SELECT session_id FROM relationships "
            "WHERE participant_id = ? AND other_participant_id = ?",
            ("test-agent", "bob"),
        ) as cursor:
            row = await cursor.fetchone()
        assert row[0] == "legacy"
        # interaction id returned; no error
        assert iid

    async def test_explicit_session_id_round_trip(
        self, rel_memory: RelationshipMemory,
    ):
        await rel_memory.record_interaction(
            "bob", "chat", session_id="run-a",
        )
        async with rel_memory._ensure_db().execute(
            "SELECT session_id FROM relationships "
            "WHERE participant_id = ? AND other_participant_id = ?",
            ("test-agent", "bob"),
        ) as cursor:
            row = await cursor.fetchone()
        assert row[0] == "run-a"

    async def test_second_interaction_does_not_overwrite_session_id(
        self, rel_memory: RelationshipMemory,
    ):
        # The first interaction stamps the row's session_id; a later
        # interaction with a different value MUST NOT overwrite it.
        # The relationships row is a stable per-pair identity; the
        # write-path-only Phase 1 contract is that the column tracks
        # the *first-seen* session id, mirroring how
        # ``last_interaction_at`` updates while ``trust_score`` does
        # not on a bare record_interaction.  This is the per-row
        # storage tag — per-interaction session id lives on the
        # interactions table once Phase 2's recall path needs it.
        await rel_memory.record_interaction(
            "bob", "chat", session_id="run-a",
        )
        await rel_memory.record_interaction(
            "bob", "chat", session_id="run-b",
        )
        async with rel_memory._ensure_db().execute(
            "SELECT session_id FROM relationships "
            "WHERE participant_id = ? AND other_participant_id = ?",
            ("test-agent", "bob"),
        ) as cursor:
            row = await cursor.fetchone()
        # Phase 1 contract: row.session_id is the *first-seen* id.
        assert row[0] == "run-a"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
