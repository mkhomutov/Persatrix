"""ISSUE-0085 PR 3 — the epoch axis has no carve-out (the inversion gate).

PR 3 added a strict, unconditional ``AND epoch_id = ?`` to every
per-request recall and write path.  This file is the epoch-axis mirror of
:mod:`test_principal_legacy_carveout`, *inverted* to assert the property
the epoch plan names explicitly: **the epoch axis has no carve-out**.  The
session ``legacy`` carve-out (which exists *for* room-continuity) can no
longer bridge two epochs — a fresh epoch must see nothing, even a
``session_id='legacy'`` row.

Why this is distinct from :mod:`test_epoch_scope`
-------------------------------------------------
That suite neutralises the *session* axis with ``sessions="*"`` to isolate
the *epoch* axis.  This suite does the opposite: it drives the **default
``sessions=None`` recall path** — the one that auto-unions the ``legacy``
carve-out via ``_resolve_session_list`` — and uses a *different* active
session on the reader (``session_scope("run-b")``) so that a row tagged
``session_id="legacy"`` is session-visible **only** through the carve-out.
If the epoch filter were ever dropped from that path, or grew a carve-out
of its own, the session carve-out would re-open as a cross-epoch bridge
and these tests fail — re-opening the structural half of F-3.

Scope note: agent-global maintenance sweeps (eviction / retention /
janitor / facts-prune / GDPR erasure) are *not* covered here — they
already skip the principal filter and the epoch axis inherits the same
deferral (a capacity-policy decision, not a per-request read path).  This
gate covers the load-bearing prompt path: the persona-memory recall +
mutation tiers.
"""

from __future__ import annotations

import pytest

from agents.epoch_id import DEFAULT_EPOCH_ID, epoch_scope
from agents.memory.episodic import EpisodicMemory
from agents.memory.facts import FactStore
from agents.memory.relationship import RelationshipMemory
from agents.session_id import LEGACY_SESSION_ID, session_scope

# A reader whose *active* session differs from ``legacy`` so a
# ``session_id="legacy"`` row is session-visible only via the carve-out.
_READER_SESSION = "run-b"


# ─── Episodic ───────────────────────────────────────────────


class TestEpisodicEpochNoCarveout:
    @pytest.fixture
    async def mem(self):
        mem = EpisodicMemory(agent_id="a", db_path=":memory:")
        await mem.initialize()
        yield mem
        await mem.close()

    async def test_fresh_epoch_cannot_read_legacy_row(
        self, mem: EpisodicMemory,
    ) -> None:
        with epoch_scope("run-1"):
            await mem.store_episode(
                "alpha legacy secret", {}, session_id=LEGACY_SESSION_ID,
            )
        # Fresh epoch, default recall (session carve-out unions 'legacy').
        with epoch_scope("run-2"), session_scope(_READER_SESSION):
            assert await mem.recall("") == []
        # Owner sees it through the very same carve-out path.
        with epoch_scope("run-1"), session_scope(_READER_SESSION):
            assert len(await mem.recall("")) == 1

    async def test_fresh_epoch_cannot_read_live_baseline(
        self, mem: EpisodicMemory,
    ) -> None:
        # Pre-migration baseline shape: (session='legacy', epoch='live').
        await mem.store_episode(
            "pre-rfc baseline", {}, session_id=LEGACY_SESSION_ID,
        )
        with epoch_scope("run-1"), session_scope(_READER_SESSION):
            assert await mem.recall("") == []
        # Positive control: the 'live' epoch (no scope) DOES see the
        # baseline through the very same carve-out path, so the empty
        # result above is the epoch boundary at work — not a baseline row
        # that silently failed to store.
        with session_scope(_READER_SESSION):
            assert len(await mem.recall("")) == 1

    async def test_fresh_write_does_not_reach_live_baseline(
        self, mem: EpisodicMemory,
    ) -> None:
        # run-1 writes under the legacy *session* — it must be tagged
        # epoch='run-1', NOT the shared 'live' baseline.
        with epoch_scope("run-1"):
            await mem.store_episode(
                "run-1 legacy write", {}, session_id=LEGACY_SESSION_ID,
            )
        # The 'live' epoch (no scope) sees an empty baseline.
        with session_scope(_READER_SESSION):
            assert await mem.recall("") == []


# ─── Notes ──────────────────────────────────────────────────


class TestNotesEpochNoCarveout:
    @pytest.fixture
    async def mem(self):
        mem = EpisodicMemory(agent_id="a", db_path=":memory:")
        await mem.initialize()
        yield mem
        await mem.close()

    async def test_fresh_epoch_cannot_read_legacy_note(
        self, mem: EpisodicMemory,
    ) -> None:
        store = mem._ensure_note_store()
        with epoch_scope("run-1"):
            await store.store_note("t", "alpha", session_id=LEGACY_SESSION_ID)
        with epoch_scope("run-2"), session_scope(_READER_SESSION):
            assert await store.recall_notes("") == []
        with epoch_scope("run-1"), session_scope(_READER_SESSION):
            assert len(await store.recall_notes("")) == 1

    async def test_fresh_epoch_cannot_mutate_legacy_note(
        self, mem: EpisodicMemory,
    ) -> None:
        store = mem._ensure_note_store()
        with epoch_scope("run-1"):
            note_id = await store.store_note(
                "t", "alpha", session_id=LEGACY_SESSION_ID,
            )
        # The notes mutation surface carries the session legacy carve-out
        # (session_id IN (active, legacy)); the epoch filter must still
        # block a fresh epoch from reaching the row through it.
        with epoch_scope("run-2"):
            assert await store.update_note(note_id, "tampered") is False
            assert await store.delete_note(note_id) is False
        with epoch_scope("run-1"):
            assert await store.update_note(note_id, "edited") is True


# ─── Facts ──────────────────────────────────────────────────


class TestFactsEpochNoCarveout:
    @pytest.fixture
    async def store(self):
        store = FactStore(agent_id="a", db_path=":memory:")
        await store.initialize()
        yield store
        await store.close()

    async def test_fresh_epoch_cannot_read_legacy_fact(
        self, store: FactStore,
    ) -> None:
        with epoch_scope("run-1"):
            await store.store(
                subject="bob", predicate="lives_in", object="NYC",
                source_interaction_id="ix", asserted_at=1000.0,
                session_id=LEGACY_SESSION_ID,
            )
        with epoch_scope("run-2"), session_scope(_READER_SESSION):
            assert await store.recall(subject="bob") == []
        with epoch_scope("run-1"), session_scope(_READER_SESSION):
            assert [f.object for f in await store.recall(subject="bob")] == ["NYC"]

    async def test_fresh_legacy_write_does_not_supersede_via_carveout(
        self, store: FactStore,
    ) -> None:
        """The supersede older-sweep spans ``session_id IN (session,
        legacy)`` — the carve-out. A fresh epoch's newer legacy write
        must NOT retract the owner-epoch's legacy fact through it.
        """
        with epoch_scope("run-1"):
            await store.store(
                subject="bob", predicate="lives_in", object="NYC",
                source_interaction_id="ix-a", asserted_at=1000.0,
                session_id=LEGACY_SESSION_ID,
            )
        with epoch_scope("run-2"):
            await store.store(
                subject="bob", predicate="lives_in", object="LA",
                source_interaction_id="ix-b", asserted_at=2000.0,
                session_id=LEGACY_SESSION_ID,
            )
        with epoch_scope("run-1"), session_scope(_READER_SESSION):
            assert [f.object for f in await store.recall(subject="bob")] == ["NYC"]


# ─── Relationship ───────────────────────────────────────────


class TestRelationshipEpochNoCarveout:
    @pytest.fixture
    async def rel(self):
        rel = RelationshipMemory(agent_id="a", db_path=":memory:")
        await rel.initialize()
        yield rel
        await rel.close()

    async def test_fresh_epoch_cannot_read_legacy_interaction(
        self, rel: RelationshipMemory,
    ) -> None:
        with epoch_scope("run-1"):
            await rel.record_interaction(
                "peer", "chat", session_id=LEGACY_SESSION_ID,
            )
        with epoch_scope("run-2"), session_scope(_READER_SESSION):
            summary = await rel.get_relationship_summary("peer")
            assert summary.interaction_count == 0
            assert summary.recent_interactions == []
        with epoch_scope("run-1"), session_scope(_READER_SESSION):
            summary_a = await rel.get_relationship_summary("peer")
            assert summary_a.interaction_count == 1


# ─── Session carve-out remains intact within an epoch ───────


class TestSessionCarveoutStillWorksWithinEpoch:
    async def test_legacy_visible_across_sessions_same_epoch(self) -> None:
        """Guard against over-correction: the session carve-out must still
        do its job — a ``legacy`` row stays visible from a *different*
        session of the *same* epoch (the pre-RFC-upgrade dementia-test
        surface is not collateral damage of the epoch filter).
        """
        mem = EpisodicMemory(agent_id="a", db_path=":memory:")
        await mem.initialize()
        try:
            with epoch_scope("run-1"):
                await mem.store_episode(
                    "carry-forward", {}, session_id=LEGACY_SESSION_ID,
                )
            with epoch_scope("run-1"), session_scope("a-much-later-run"):
                got = await mem.recall("")
            assert len(got) == 1, "session carve-out must survive within an epoch"
        finally:
            await mem.close()

    async def test_default_epoch_constant(self) -> None:
        assert DEFAULT_EPOCH_ID == "live"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
