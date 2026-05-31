"""ISSUE-0085 PR 3 — cross-epoch (run/test) isolation across tiers.

The epoch analogue of :mod:`test_principal_scope`.  Each test drives the
active epoch with :func:`agents.epoch_id.epoch_scope` on a **single
shared tier instance** — the real ISSUE-0085 scenario where one persona
process fields two CI jobs' (or a rerun's) writes through the same
long-lived tier object.

Every read uses ``sessions="*"`` to neutralise the *session* axis so the
*epoch* axis is the only discriminator under test — proving the two
scopes are independent and that epoch isolation holds even on the
CLI/debug "all sessions" path (epoch has no ``"*"`` of its own).

The load-bearing property (same as the principal axis, vs. the session
axis): **strict equality, no carve-out**.  A row written under one epoch
is invisible to every other, including across the default-epoch ``live``
boundary — there is no ``legacy``-style always-visible epoch.  This is
the structural half of the F-3 fix: a rerun reusing ``--user alice``
under a fresh epoch inherits none of the prior run's episodes,
relationships, or person-facts.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from agents.epoch_id import DEFAULT_EPOCH_ID, epoch_scope
from agents.memory.episodic import EpisodicMemory
from agents.memory.facts import FactStore
from agents.memory.relationship import RelationshipMemory
from agents.memory.relationship_types import _DEFAULT_TRUST

# ─── Episodic ───────────────────────────────────────────────


class TestEpisodicEpochIsolation:
    @pytest.fixture
    async def mem(self):
        mem = EpisodicMemory(agent_id="a", db_path=":memory:")
        await mem.initialize()
        yield mem
        await mem.close()

    async def test_write_a_invisible_to_b(self, mem: EpisodicMemory) -> None:
        with epoch_scope("run-a"):
            await mem.store_episode("alpha secret", {}, session_id="legacy")
        with epoch_scope("run-b"):
            got = await mem.recall("", sessions="*")
        assert got == []

    async def test_write_a_visible_to_a(self, mem: EpisodicMemory) -> None:
        with epoch_scope("run-a"):
            await mem.store_episode("alpha secret", {}, session_id="legacy")
            got = await mem.recall("", sessions="*")
        assert len(got) == 1

    async def test_default_epoch_not_bridged_to_named(
        self, mem: EpisodicMemory,
    ) -> None:
        """No carve-out: a ``live`` (default) row is NOT visible to a
        named epoch, and a named row is not visible to ``live``.
        """
        # Written with no scope active → DEFAULT_EPOCH_ID ('live').
        await mem.store_episode("live row", {}, session_id="legacy")
        with epoch_scope("run-a"):
            assert await mem.recall("", sessions="*") == []
        # And the named-epoch row is invisible to the default epoch.
        with epoch_scope("run-a"):
            await mem.store_episode("run row", {}, session_id="legacy")
        got = await mem.recall("", sessions="*")  # live epoch
        assert len(got) == 1
        assert got[0].summary == "live row"


# ─── Notes ──────────────────────────────────────────────────


class TestNotesEpochIsolation:
    @pytest.fixture
    async def mem(self):
        mem = EpisodicMemory(agent_id="a", db_path=":memory:")
        await mem.initialize()
        yield mem
        await mem.close()

    async def test_recall_isolated(self, mem: EpisodicMemory) -> None:
        store = mem._ensure_note_store()
        with epoch_scope("run-a"):
            await store.store_note("t", "alpha note", session_id="legacy")
        with epoch_scope("run-b"):
            assert await store.recall_notes("", sessions="*") == []
        with epoch_scope("run-a"):
            assert len(await store.recall_notes("", sessions="*")) == 1

    async def test_mutation_cross_epoch_denied(
        self, mem: EpisodicMemory,
    ) -> None:
        store = mem._ensure_note_store()
        with epoch_scope("run-a"):
            note_id = await store.store_note(
                "t", "alpha note", session_id="legacy",
            )
        with epoch_scope("run-b"):
            assert await store.update_note(note_id, "tampered") is False
            assert await store.delete_note(note_id) is False
            assert await store.count_notes() == 0
        # The row survives untouched for run-a.
        with epoch_scope("run-a"):
            assert await store.count_notes() == 1
            assert await store.update_note(note_id, "edited") is True


# ─── Facts ──────────────────────────────────────────────────


class TestFactsEpochIsolation:
    @pytest.fixture
    async def store(self):
        store = FactStore(agent_id="a", db_path=":memory:")
        await store.initialize()
        yield store
        await store.close()

    async def test_recall_isolated(self, store: FactStore) -> None:
        with epoch_scope("run-a"):
            await store.store(
                subject="bob", predicate="lives_in", object="NYC",
                source_interaction_id="ix", asserted_at=1000.0,
                session_id="legacy",
            )
        with epoch_scope("run-b"):
            assert await store.recall(subject="bob", sessions="*") == []
        with epoch_scope("run-a"):
            got = await store.recall(subject="bob", sessions="*")
        assert [f.object for f in got] == ["NYC"]

    async def test_cross_epoch_supersede_does_not_retract(
        self, store: FactStore,
    ) -> None:
        """A newer write under run-b on the same (subject, predicate)
        must NOT supersede run-a's live fact — the supersession chain
        is epoch-scoped.
        """
        with epoch_scope("run-a"):
            await store.store(
                subject="bob", predicate="lives_in", object="NYC",
                source_interaction_id="ix-a", asserted_at=1000.0,
                session_id="legacy",
            )
        with epoch_scope("run-b"):
            await store.store(
                subject="bob", predicate="lives_in", object="LA",
                source_interaction_id="ix-b", asserted_at=2000.0,
                session_id="legacy",
            )
        # run-a's fact is still live (not marked superseded_by).
        with epoch_scope("run-a"):
            got = await store.recall(subject="bob", sessions="*")
        assert [f.object for f in got] == ["NYC"]
        # run-b sees only its own.
        with epoch_scope("run-b"):
            got_b = await store.recall(subject="bob", sessions="*")
        assert [f.object for f in got_b] == ["LA"]

    async def test_manual_supersede_cross_epoch_denied(
        self, store: FactStore,
    ) -> None:
        """``FactStore.supersede`` is epoch-scoped, symmetric with the
        automatic supersession chain: a run-b caller cannot retract a
        run-a fact by id, even though both rows share the agent.
        """
        with epoch_scope("run-a"):
            fact_a = await store.store(
                subject="bob", predicate="lives_in", object="NYC",
                source_interaction_id="ix-a", asserted_at=1000.0,
                session_id="legacy",
            )
        with epoch_scope("run-b"):
            fact_b = await store.store(
                subject="carol", predicate="lives_in", object="LA",
                source_interaction_id="ix-b", asserted_at=2000.0,
                session_id="legacy",
            )
            # run-b attempts to retract run-a's fact by id → no-op.
            retracted = await store.supersede(fact_a, fact_b)
        assert retracted is False
        # run-a's fact is still live (untouched by the foreign retract).
        with epoch_scope("run-a"):
            got = await store.recall(subject="bob", sessions="*")
        assert [f.object for f in got] == ["NYC"]


# ─── Relationship ───────────────────────────────────────────


class TestRelationshipEpochIsolation:
    @pytest.fixture
    async def rel(self):
        rel = RelationshipMemory(agent_id="a", db_path=":memory:")
        await rel.initialize()
        yield rel
        await rel.close()

    async def test_trust_and_summary_isolated(
        self, rel: RelationshipMemory,
    ) -> None:
        with epoch_scope("run-a"):
            await rel.record_interaction(
                "peer", "chat", outcome="ok", sentiment=0.5,
                session_id="legacy",
            )
            await rel.update_trust("peer", 0.2, "warmed up")

        # run-b sees the neutral default + an empty summary.
        with epoch_scope("run-b"):
            assert await rel.get_trust("peer", sessions="*") == 0.5
            summary_b = await rel.get_relationship_summary(
                "peer", sessions="*",
            )
            assert summary_b.interaction_count == 0
            assert summary_b.recent_interactions == []
            assert await rel.get_all_relationships(sessions="*") == []

        # run-a still sees its interaction history.
        with epoch_scope("run-a"):
            summary_a = await rel.get_relationship_summary(
                "peer", sessions="*",
            )
            assert summary_a.interaction_count == 1
            assert len(summary_a.recent_interactions) == 1

    async def test_update_trust_first_touch_tags_active_epoch(
        self, rel: RelationshipMemory,
    ) -> None:
        """``update_trust`` as the *first* touch (no prior
        ``record_interaction``) must tag the new ``relationships`` row with
        the active epoch, not the column default ``live`` — symmetric with
        the principal axis review-H1 regression.
        """
        with epoch_scope("run-a"):
            await rel.update_trust("peer", 0.2, "warmed up")
            assert await rel.get_trust("peer", sessions="*") == pytest.approx(0.7)
        # The default ``live`` epoch must NOT see run-a's trust.
        assert await rel.get_trust("peer", sessions="*") == _DEFAULT_TRUST

    async def test_record_interaction_two_writers_isolated(
        self, rel: RelationshipMemory,
    ) -> None:
        """Two epochs recording interactions with the *same* participant
        tuple must not share the aggregate ``relationships`` row — the
        epoch_id-in-PK guarantee (migration v12), symmetric with the
        principal axis review-H2 regression.
        """
        with epoch_scope("run-a"):
            await rel.record_interaction(
                "peer", "chat", outcome="ok", sentiment=0.5, session_id="legacy",
            )
            await rel.update_trust("peer", 0.2, "a-warmed")
            a_trust = await rel.get_trust("peer", sessions="*")
        with epoch_scope("run-b"):
            await rel.record_interaction(
                "peer", "chat", outcome="bad", sentiment=-0.5, session_id="legacy",
            )
            await rel.update_trust("peer", -0.2, "b-cooled")
            b_trust = await rel.get_trust("peer", sessions="*")
        # run-b sees its own (independent) row, not the neutral default.
        assert b_trust == pytest.approx(0.3)
        # run-a's trust is untouched by run-b's write.
        with epoch_scope("run-a"):
            assert await rel.get_trust("peer", sessions="*") == pytest.approx(a_trust)
            assert await rel.get_trust("peer", sessions="*") == pytest.approx(0.7)

    async def test_apply_decay_is_epoch_scoped(
        self, rel: RelationshipMemory,
    ) -> None:
        """``apply_decay`` must only decay rows under the active epoch —
        a run-b maintenance pass cannot move run-a's trust.
        """
        with epoch_scope("run-a"):
            await rel.update_trust("peer", 0.2, "a")  # → 0.7
        with epoch_scope("run-b"):
            await rel.update_trust("peer", 0.2, "b")  # → 0.7
            await rel.apply_decay(decay_rate=0.5)      # run-b only
            b_after = await rel.get_trust("peer", sessions="*")
        with epoch_scope("run-a"):
            a_after = await rel.get_trust("peer", sessions="*")
        assert b_after < 0.7  # run-b decayed toward neutral
        assert a_after == pytest.approx(0.7)  # run-a untouched


# ─── Procedural (facade path) ───────────────────────────────


class TestProceduralEpochIsolation:
    async def test_retrieve_isolated(self) -> None:
        from agents.memory.facade import MemoryStore

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "m.db")
            store = MemoryStore(agent_id="a", db_path=path)
            await store.initialize()
            try:
                with epoch_scope("run-a"):
                    await store.store_procedure(
                        "do.thing", "step one", confidence=0.9,
                    )
                with epoch_scope("run-b"):
                    assert await store.retrieve_procedures(
                        sessions="*",
                    ) == []
                with epoch_scope("run-a"):
                    got = await store.retrieve_procedures(sessions="*")
                    assert len(got) == 1
            finally:
                await store.close()

    async def test_store_same_key_two_epochs_isolated(self) -> None:
        """Two epochs storing a procedure under the *same* key must each
        own a distinct row — run-b's store must neither mutate run-a's
        procedure nor be silently dropped by the refresh short-circuit
        (symmetric with the principal axis refresh-scoping fix).
        """
        from agents.memory.facade import MemoryStore

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "m.db")
            store = MemoryStore(agent_id="a", db_path=path)
            await store.initialize()
            try:
                with epoch_scope("run-a"):
                    await store.store_procedure(
                        "do.thing", "a-step", confidence=0.9,
                    )
                with epoch_scope("run-b"):
                    await store.store_procedure(
                        "do.thing", "b-step", confidence=0.4,
                    )
                    got_b = await store.retrieve_procedures(sessions="*")
                assert [e.content for e in got_b] == ["b-step"]
                with epoch_scope("run-a"):
                    got_a = await store.retrieve_procedures(sessions="*")
                assert [e.content for e in got_a] == ["a-step"]
            finally:
                await store.close()


# ─── Single-world: behaviour unchanged ──────────────────────


class TestSingleWorldUnchanged:
    async def test_no_scope_resolves_live_and_round_trips(self) -> None:
        """With no ``epoch_scope`` and env unset, writes + reads both
        resolve to ``live`` so the row round-trips — single-world
        deployments are unaffected.
        """
        mem = EpisodicMemory(agent_id="a", db_path=":memory:")
        await mem.initialize()
        try:
            assert mem._active_epoch_id == DEFAULT_EPOCH_ID
            await mem.store_episode("plain", {}, session_id="legacy")
            got = await mem.recall("", sessions="*")
            assert len(got) == 1
        finally:
            await mem.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
