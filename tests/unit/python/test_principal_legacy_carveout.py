"""ISSUE-0081 PR 4 — legacy carve-out is principal-bounded (the closeout gate).

PR 3 added a strict, unconditional ``AND principal_id = ?`` to every
per-request recall and write path. A consequence the issue's PR 4 names
as its explicit TDD gate — *"a foreign tenant can neither read nor write
``legacy`` rows"* — is that the session ``legacy`` carve-out can no
longer bridge tenants. This file is that gate: a permanent regression pin
proving the carve-out is principal-bounded across every tier.

Why this is distinct from ``test_principal_scope.py``
-----------------------------------------------------
That suite neutralises the *session* axis with ``sessions="*"`` to isolate
the *principal* axis. This suite does the opposite: it drives the
**default ``sessions=None`` recall path** — the one that auto-unions the
``legacy`` carve-out via ``_resolve_session_list`` — and uses a *different*
active session on the reader (``session_scope("run-b")``) so that a row
tagged ``session_id="legacy"`` is session-visible **only** through the
carve-out. If the principal filter were ever dropped from that path, the
carve-out would re-open as a cross-tenant bridge and these tests fail.

Scope note (ISSUE-0081 §C/§D amendments): agent-global maintenance sweeps
(eviction / retention / janitor / facts-prune / GDPR erasure) and the
cross-agent shared-pool tier are *not* covered here — they are capacity /
erasure / collaboration policy deferred to RFC 0039, not per-request
read-confidentiality surfaces. This gate covers the load-bearing prompt
path: the four persona-memory recall + mutation tiers.
"""

from __future__ import annotations

import pytest

from agents.memory.episodic import EpisodicMemory
from agents.memory.facts import FactStore
from agents.memory.relationship import RelationshipMemory
from agents.principal_id import DEFAULT_PRINCIPAL_ID, principal_scope
from agents.session_id import LEGACY_SESSION_ID, session_scope

# A reader whose *active* session differs from ``legacy`` so a
# ``session_id="legacy"`` row is session-visible only via the carve-out.
_READER_SESSION = "run-b"


# ─── Episodic ───────────────────────────────────────────────


class TestEpisodicLegacyCarveout:
    @pytest.fixture
    async def mem(self):
        mem = EpisodicMemory(agent_id="a", db_path=":memory:")
        await mem.initialize()
        yield mem
        await mem.close()

    async def test_foreign_tenant_cannot_read_legacy_row(
        self, mem: EpisodicMemory,
    ) -> None:
        with principal_scope("tenant-a"):
            await mem.store_episode(
                "alpha legacy secret", {}, session_id=LEGACY_SESSION_ID,
            )
        # Foreign tenant, default recall (carve-out unions 'legacy').
        with principal_scope("tenant-b"), session_scope(_READER_SESSION):
            assert await mem.recall("") == []
        # Owner sees it through the very same carve-out path.
        with principal_scope("tenant-a"), session_scope(_READER_SESSION):
            assert len(await mem.recall("")) == 1

    async def test_foreign_tenant_cannot_read_local_baseline(
        self, mem: EpisodicMemory,
    ) -> None:
        # Pre-migration baseline shape: (session='legacy', principal='local').
        await mem.store_episode(
            "pre-rfc baseline", {}, session_id=LEGACY_SESSION_ID,
        )
        with principal_scope("tenant-a"), session_scope(_READER_SESSION):
            assert await mem.recall("") == []

    async def test_foreign_write_does_not_reach_local_baseline(
        self, mem: EpisodicMemory,
    ) -> None:
        # Tenant-b writes under the legacy *session* — it must be tagged
        # principal='tenant-b', NOT the shared 'local' baseline.
        with principal_scope("tenant-b"):
            await mem.store_episode(
                "tenant-b legacy write", {}, session_id=LEGACY_SESSION_ID,
            )
        # The 'local' principal (no scope) sees an empty baseline.
        with session_scope(_READER_SESSION):
            assert await mem.recall("") == []


# ─── Notes ──────────────────────────────────────────────────


class TestNotesLegacyCarveout:
    @pytest.fixture
    async def mem(self):
        mem = EpisodicMemory(agent_id="a", db_path=":memory:")
        await mem.initialize()
        yield mem
        await mem.close()

    async def test_foreign_tenant_cannot_read_legacy_note(
        self, mem: EpisodicMemory,
    ) -> None:
        store = mem._ensure_note_store()
        with principal_scope("tenant-a"):
            await store.store_note("t", "alpha", session_id=LEGACY_SESSION_ID)
        with principal_scope("tenant-b"), session_scope(_READER_SESSION):
            assert await store.recall_notes("") == []
        with principal_scope("tenant-a"), session_scope(_READER_SESSION):
            assert len(await store.recall_notes("")) == 1

    async def test_foreign_tenant_cannot_mutate_legacy_note(
        self, mem: EpisodicMemory,
    ) -> None:
        store = mem._ensure_note_store()
        with principal_scope("tenant-a"):
            note_id = await store.store_note(
                "t", "alpha", session_id=LEGACY_SESSION_ID,
            )
        # The notes mutation surface carries the session legacy carve-out
        # (session_id IN (active, legacy)); the principal filter must still
        # block a foreign tenant from reaching the row through it.
        with principal_scope("tenant-b"):
            assert await store.update_note(note_id, "tampered") is False
            assert await store.delete_note(note_id) is False
        with principal_scope("tenant-a"):
            assert await store.update_note(note_id, "edited") is True


# ─── Facts ──────────────────────────────────────────────────


class TestFactsLegacyCarveout:
    @pytest.fixture
    async def store(self):
        store = FactStore(agent_id="a", db_path=":memory:")
        await store.initialize()
        yield store
        await store.close()

    async def test_foreign_tenant_cannot_read_legacy_fact(
        self, store: FactStore,
    ) -> None:
        with principal_scope("tenant-a"):
            await store.store(
                subject="bob", predicate="lives_in", object="NYC",
                source_interaction_id="ix", asserted_at=1000.0,
                session_id=LEGACY_SESSION_ID,
            )
        with principal_scope("tenant-b"), session_scope(_READER_SESSION):
            assert await store.recall(subject="bob") == []
        with principal_scope("tenant-a"), session_scope(_READER_SESSION):
            assert [f.object for f in await store.recall(subject="bob")] == ["NYC"]

    async def test_foreign_legacy_write_does_not_supersede_via_carveout(
        self, store: FactStore,
    ) -> None:
        """The supersede older-sweep spans ``session_id IN (session,
        legacy)`` — the carve-out. A foreign tenant's newer legacy write
        must NOT retract the owner's legacy fact through it.
        """
        with principal_scope("tenant-a"):
            await store.store(
                subject="bob", predicate="lives_in", object="NYC",
                source_interaction_id="ix-a", asserted_at=1000.0,
                session_id=LEGACY_SESSION_ID,
            )
        with principal_scope("tenant-b"):
            await store.store(
                subject="bob", predicate="lives_in", object="LA",
                source_interaction_id="ix-b", asserted_at=2000.0,
                session_id=LEGACY_SESSION_ID,
            )
        with principal_scope("tenant-a"), session_scope(_READER_SESSION):
            assert [f.object for f in await store.recall(subject="bob")] == ["NYC"]


# ─── Relationship ───────────────────────────────────────────


class TestRelationshipLegacyCarveout:
    @pytest.fixture
    async def rel(self):
        rel = RelationshipMemory(agent_id="a", db_path=":memory:")
        await rel.initialize()
        yield rel
        await rel.close()

    async def test_foreign_tenant_cannot_read_legacy_interaction(
        self, rel: RelationshipMemory,
    ) -> None:
        with principal_scope("tenant-a"):
            await rel.record_interaction(
                "peer", "chat", session_id=LEGACY_SESSION_ID,
            )
        with principal_scope("tenant-b"), session_scope(_READER_SESSION):
            summary = await rel.get_relationship_summary("peer")
            assert summary.interaction_count == 0
            assert summary.recent_interactions == []
        with principal_scope("tenant-a"), session_scope(_READER_SESSION):
            summary_a = await rel.get_relationship_summary("peer")
            assert summary_a.interaction_count == 1


# ─── Carve-out remains intact within a principal ────────────


class TestCarveoutStillWorksWithinPrincipal:
    async def test_legacy_visible_across_sessions_same_principal(self) -> None:
        """Guard against over-correction: the carve-out must still do its
        job — a ``legacy`` row stays visible from a *different* session of
        the *same* principal (the pre-RFC-upgrade dementia-test surface).
        """
        mem = EpisodicMemory(agent_id="a", db_path=":memory:")
        await mem.initialize()
        try:
            with principal_scope("tenant-a"):
                await mem.store_episode(
                    "carry-forward", {}, session_id=LEGACY_SESSION_ID,
                )
            with principal_scope("tenant-a"), session_scope("a-much-later-run"):
                got = await mem.recall("")
            assert len(got) == 1, "carve-out must survive within a principal"
        finally:
            await mem.close()

    async def test_default_principal_constant(self) -> None:
        assert DEFAULT_PRINCIPAL_ID == "local"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
