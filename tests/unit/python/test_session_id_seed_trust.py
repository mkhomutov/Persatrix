"""
Tests for RFC 0031 PR plan PR 4 finding #2: ``seed_trust`` must accept and
persist ``session_id`` so a config-seeded relationship row carries the
caller's session id rather than the column-default ``"legacy"``.

Without the fix, the seed path inserts ``session_id='legacy'`` (column
default) and the first real ``record_interaction`` under
``PERSATRIX_SESSION_ID=run-a`` hits the ON CONFLICT branch which
deliberately preserves the first-seen value — so the relationship row
stays tagged ``'legacy'``.  MT-SESSION-001 Step 7 ("Relationships row
``session_id`` is ``run-a``") fails in that seed-before-record sequence
for any persona that pre-declares the peer in its ``relationships:``
config block.
"""

from __future__ import annotations

import pytest

from agents.memory.relationship import RelationshipMemory


@pytest.fixture
async def rel_memory():
    mem = RelationshipMemory(agent_id="ember-owl", db_path=":memory:")
    yield mem
    await mem.close()


class TestSeedTrustSessionID:
    """RFC 0031 PR plan PR 4 finding #2 regression suite."""

    async def test_initialize_threads_session_id_into_seed(
        self, rel_memory: RelationshipMemory,
    ) -> None:
        await rel_memory.initialize(
            config_relationships=[{"agent_id": "bob", "trust_level": 0.9}],
            session_id="run-a",
        )
        async with rel_memory._ensure_db().execute(
            "SELECT session_id FROM relationships "
            "WHERE participant_id = ? AND other_participant_id = ?",
            ("ember-owl", "bob"),
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None, "seed_trust did not insert a row"
        assert row[0] == "run-a", (
            f"seed must tag the row with the caller-supplied session_id; "
            f"got {row[0]!r}"
        )

    async def test_initialize_defaults_to_legacy_when_unset(
        self, rel_memory: RelationshipMemory,
    ) -> None:
        # The kwarg-omitted call path keeps the existing public surface
        # working — existing tests that pass no session_id must still
        # observe the column-default carve-out.
        await rel_memory.initialize(
            config_relationships=[{"agent_id": "bob", "trust_level": 0.9}],
        )
        async with rel_memory._ensure_db().execute(
            "SELECT session_id FROM relationships "
            "WHERE participant_id = ? AND other_participant_id = ?",
            ("ember-owl", "bob"),
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "legacy"

    async def test_seed_then_record_interaction_preserves_run_a(
        self, rel_memory: RelationshipMemory,
    ) -> None:
        # The MT-SESSION-001 Step 7 scenario: seed the peer at boot
        # under run-a, then record_interaction under run-a — the row
        # tag must be ``run-a``, not ``legacy``.  Without the seed-side
        # fix, the seed inserted ``legacy``, the conflict branch
        # preserved it, and Step 7 silently failed.
        await rel_memory.initialize(
            config_relationships=[{"agent_id": "bob", "trust_level": 0.9}],
            session_id="run-a",
        )
        await rel_memory.record_interaction(
            "bob", "chat", session_id="run-a",
        )
        async with rel_memory._ensure_db().execute(
            "SELECT session_id FROM relationships "
            "WHERE participant_id = ? AND other_participant_id = ?",
            ("ember-owl", "bob"),
        ) as cursor:
            row = await cursor.fetchone()
        assert row[0] == "run-a", (
            f"seed-then-record must keep run-a tag; got {row[0]!r}"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
