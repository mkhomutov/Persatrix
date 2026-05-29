"""ISSUE-0081 PR 3 — cross-principal (tenant) isolation across tiers.

The tenant analogue of the session-scope suites.  Each test drives the
active principal with :func:`agents.principal_id.principal_scope` on a
**single shared tier instance** — the real ISSUE-0081 scenario where one
persona process fields two tenants' conversations through the same
long-lived tier object.

Every read uses ``sessions="*"`` to neutralise the *session* axis so the
*principal* axis is the only discriminator under test — proving the two
scopes are independent and that principal isolation holds even on the
CLI/debug "all sessions" path.

The load-bearing property (vs. the session axis): **strict equality, no
carve-out**.  A row owned by one principal is invisible to every other,
including across the default-principal ``local`` boundary — there is no
``legacy``-style always-visible tenant.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from agents.memory.episodic import EpisodicMemory
from agents.memory.facts import FactStore
from agents.memory.relationship import RelationshipMemory
from agents.principal_id import DEFAULT_PRINCIPAL_ID, principal_scope

# ─── Episodic ───────────────────────────────────────────────


class TestEpisodicPrincipalIsolation:
    @pytest.fixture
    async def mem(self):
        mem = EpisodicMemory(agent_id="a", db_path=":memory:")
        await mem.initialize()
        yield mem
        await mem.close()

    async def test_write_a_invisible_to_b(self, mem: EpisodicMemory) -> None:
        with principal_scope("tenant-a"):
            await mem.store_episode("alpha secret", {}, session_id="legacy")
        with principal_scope("tenant-b"):
            got = await mem.recall("", sessions="*")
        assert got == []

    async def test_write_a_visible_to_a(self, mem: EpisodicMemory) -> None:
        with principal_scope("tenant-a"):
            await mem.store_episode("alpha secret", {}, session_id="legacy")
            got = await mem.recall("", sessions="*")
        assert len(got) == 1

    async def test_default_principal_not_bridged_to_named(
        self, mem: EpisodicMemory,
    ) -> None:
        """No carve-out: a ``local`` (default) row is NOT visible to a
        named tenant, and a named row is not visible to ``local``.
        """
        # Written with no scope active → DEFAULT_PRINCIPAL_ID ('local').
        await mem.store_episode("local row", {}, session_id="legacy")
        with principal_scope("tenant-a"):
            assert await mem.recall("", sessions="*") == []
        # And the named-tenant row is invisible to the default principal.
        with principal_scope("tenant-a"):
            await mem.store_episode("tenant row", {}, session_id="legacy")
        got = await mem.recall("", sessions="*")  # local principal
        assert len(got) == 1
        assert got[0].summary == "local row"


# ─── Notes ──────────────────────────────────────────────────


class TestNotesPrincipalIsolation:
    @pytest.fixture
    async def mem(self):
        mem = EpisodicMemory(agent_id="a", db_path=":memory:")
        await mem.initialize()
        yield mem
        await mem.close()

    async def test_recall_isolated(self, mem: EpisodicMemory) -> None:
        store = mem._ensure_note_store()
        with principal_scope("tenant-a"):
            await store.store_note("t", "alpha note", session_id="legacy")
        with principal_scope("tenant-b"):
            assert await store.recall_notes("", sessions="*") == []
        with principal_scope("tenant-a"):
            assert len(await store.recall_notes("", sessions="*")) == 1

    async def test_mutation_cross_principal_denied(
        self, mem: EpisodicMemory,
    ) -> None:
        store = mem._ensure_note_store()
        with principal_scope("tenant-a"):
            note_id = await store.store_note(
                "t", "alpha note", session_id="legacy",
            )
        with principal_scope("tenant-b"):
            assert await store.update_note(note_id, "tampered") is False
            assert await store.delete_note(note_id) is False
            assert await store.count_notes() == 0
        # The row survives untouched for tenant-a.
        with principal_scope("tenant-a"):
            assert await store.count_notes() == 1
            assert await store.update_note(note_id, "edited") is True


# ─── Facts ──────────────────────────────────────────────────


class TestFactsPrincipalIsolation:
    @pytest.fixture
    async def store(self):
        store = FactStore(agent_id="a", db_path=":memory:")
        await store.initialize()
        yield store
        await store.close()

    async def test_recall_isolated(self, store: FactStore) -> None:
        with principal_scope("tenant-a"):
            await store.store(
                subject="bob", predicate="lives_in", object="NYC",
                source_interaction_id="ix", asserted_at=1000.0,
                session_id="legacy",
            )
        with principal_scope("tenant-b"):
            assert await store.recall(subject="bob", sessions="*") == []
        with principal_scope("tenant-a"):
            got = await store.recall(subject="bob", sessions="*")
        assert [f.object for f in got] == ["NYC"]

    async def test_cross_principal_supersede_does_not_retract(
        self, store: FactStore,
    ) -> None:
        """A newer write by tenant-b on the same (subject, predicate)
        must NOT supersede tenant-a's live fact — the supersession chain
        is principal-scoped.
        """
        with principal_scope("tenant-a"):
            await store.store(
                subject="bob", predicate="lives_in", object="NYC",
                source_interaction_id="ix-a", asserted_at=1000.0,
                session_id="legacy",
            )
        with principal_scope("tenant-b"):
            await store.store(
                subject="bob", predicate="lives_in", object="LA",
                source_interaction_id="ix-b", asserted_at=2000.0,
                session_id="legacy",
            )
        # tenant-a's fact is still live (not marked superseded_by).
        with principal_scope("tenant-a"):
            got = await store.recall(subject="bob", sessions="*")
        assert [f.object for f in got] == ["NYC"]
        # tenant-b sees only its own.
        with principal_scope("tenant-b"):
            got_b = await store.recall(subject="bob", sessions="*")
        assert [f.object for f in got_b] == ["LA"]


# ─── Relationship ───────────────────────────────────────────


class TestRelationshipPrincipalIsolation:
    @pytest.fixture
    async def rel(self):
        rel = RelationshipMemory(agent_id="a", db_path=":memory:")
        await rel.initialize()
        yield rel
        await rel.close()

    async def test_trust_and_summary_isolated(
        self, rel: RelationshipMemory,
    ) -> None:
        with principal_scope("tenant-a"):
            await rel.record_interaction(
                "peer", "chat", outcome="ok", sentiment=0.5,
                session_id="legacy",
            )
            await rel.update_trust("peer", 0.2, "warmed up")

        # tenant-b sees the neutral default + an empty summary.
        with principal_scope("tenant-b"):
            assert await rel.get_trust("peer", sessions="*") == 0.5
            summary_b = await rel.get_relationship_summary(
                "peer", sessions="*",
            )
            assert summary_b.interaction_count == 0
            assert summary_b.recent_interactions == []
            assert await rel.get_all_relationships(sessions="*") == []

        # tenant-a still sees its interaction history.
        with principal_scope("tenant-a"):
            summary_a = await rel.get_relationship_summary(
                "peer", sessions="*",
            )
            assert summary_a.interaction_count == 1
            assert len(summary_a.recent_interactions) == 1


# ─── Procedural (facade path) ───────────────────────────────


class TestProceduralPrincipalIsolation:
    async def test_retrieve_isolated(self) -> None:
        from agents.memory.facade import MemoryStore

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "m.db")
            store = MemoryStore(agent_id="a", db_path=path)
            await store.initialize()
            try:
                with principal_scope("tenant-a"):
                    await store.store_procedure(
                        "do.thing", "step one", confidence=0.9,
                    )
                with principal_scope("tenant-b"):
                    assert await store.retrieve_procedures(
                        sessions="*",
                    ) == []
                with principal_scope("tenant-a"):
                    got = await store.retrieve_procedures(sessions="*")
                    assert len(got) == 1
            finally:
                await store.close()


# ─── Single-tenant: behaviour unchanged ─────────────────────


class TestSingleTenantUnchanged:
    async def test_no_scope_resolves_local_and_round_trips(self) -> None:
        """With no ``principal_scope`` and env unset, writes + reads both
        resolve to ``local`` so the row round-trips — single-tenant
        deployments are unaffected.
        """
        mem = EpisodicMemory(agent_id="a", db_path=":memory:")
        await mem.initialize()
        try:
            assert mem._active_principal_id == DEFAULT_PRINCIPAL_ID
            await mem.store_episode("plain", {}, session_id="legacy")
            got = await mem.recall("", sessions="*")
            assert len(got) == 1
        finally:
            await mem.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
