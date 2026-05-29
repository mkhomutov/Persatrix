"""
Tests for RFC 0031 Phase 2 PR 3 — session-scoped recall on the
``facts`` tier.

PR 2 closed F-3 on episodes + notes; PR 3 extends the same §D contract
to :meth:`agents.memory.facts.FactStore.recall`.  ``facts.session_id``
already exists since migration v8 (RFC 0026); PR 3 is recall-only.

§D modes mirror the episodic + notes contract:

* ``sessions=None`` (default) → active session only, plus the always-
  visible ``legacy`` carve-out (the load-bearing dementia-test
  surface — pre-RFC fact rows persist with ``session_id='legacy'`` and
  must stay visible after the persona upgrades into v0.3.5).
* ``sessions=["a", "b"]`` → named list, plus the ``legacy`` carve-out.
* ``sessions="*"`` → no filter (CLI / debug sentinel).
* ``sessions=[]`` → ``ValueError`` (§D guard).

Active session resolved once at tier construction via
:func:`agents.session_id.resolve_session_id_silent`.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from agents.memory.facts import FactStore
from agents.session_id import LEGACY_SESSION_ID, SESSION_ID_ENV_VAR

# ─── Helpers ────────────────────────────────────────────────


async def _seed_three_session_facts(
    store: FactStore,
    *,
    subject: str = "bob",
    predicate: str = "lives_in",
) -> dict[str, str]:
    """Store one fact about ``subject`` in each of ``run-a`` / ``run-b``
    / ``legacy``.  Per ``(subject, predicate)`` symmetric latest-asserted-
    wins, the rows must use distinct predicates so all three survive
    without superseding one another.

    Returns ``{session_id: fact_id}``.
    """
    a = await store.store(
        subject=subject, predicate=predicate, object="Albuquerque",
        source_interaction_id="ix-a", asserted_at=1000.0,
        session_id="run-a",
    )
    b = await store.store(
        subject=subject, predicate="works_at", object="Acme",
        source_interaction_id="ix-b", asserted_at=1001.0,
        session_id="run-b",
    )
    legacy = await store.store(
        subject=subject, predicate="speaks_language", object="English",
        source_interaction_id="ix-l", asserted_at=1002.0,
        session_id="legacy",
    )
    return {"run-a": a, "run-b": b, "legacy": legacy}


@pytest.fixture
async def fact_store_at_run_a(monkeypatch: pytest.MonkeyPatch):
    """``FactStore`` constructed with ``PERSATRIX_SESSION_ID=run-a``."""
    monkeypatch.setenv(SESSION_ID_ENV_VAR, "run-a")
    store = FactStore(agent_id="test-agent", db_path=":memory:")
    await store.initialize()
    yield store
    await store.close()


# ─── Active-session resolution at tier construction ─────────


class TestActiveSessionResolution:
    """:class:`FactStore` resolves the active session once at __init__.

    Tier-owned so a persona-direct call (one that bypasses
    :class:`MemoryStore`) gets the same ``sessions=None`` shape as the
    facade path.
    """

    async def test_default_active_session_is_legacy_when_env_unset(
        self,
    ) -> None:
        store = FactStore(agent_id="t", db_path=":memory:")
        try:
            await store.initialize()
            assert store._active_session_id == LEGACY_SESSION_ID
        finally:
            await store.close()

    async def test_env_var_resolved_at_construction(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(SESSION_ID_ENV_VAR, "run-a")
        store = FactStore(agent_id="t", db_path=":memory:")
        try:
            await store.initialize()
            assert store._active_session_id == "run-a"
        finally:
            await store.close()

    async def test_active_session_id_immutable_after_construction(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(SESSION_ID_ENV_VAR, "run-a")
        store = FactStore(agent_id="t", db_path=":memory:")
        try:
            await store.initialize()
            monkeypatch.setenv(SESSION_ID_ENV_VAR, "run-b")
            assert store._active_session_id == "run-a"
        finally:
            await store.close()


# ─── FactStore.recall — sessions parameter ──────────────────


class TestFactRecallSessionFilter:
    async def test_default_returns_active_plus_legacy_only(
        self, fact_store_at_run_a: FactStore,
    ) -> None:
        ids = await _seed_three_session_facts(fact_store_at_run_a)
        facts = await fact_store_at_run_a.recall(subject="bob", limit=10)
        fact_ids = {f.fact_id for f in facts}
        assert ids["run-a"] in fact_ids
        assert ids["legacy"] in fact_ids
        # F-3 closer on the facts tier.
        assert ids["run-b"] not in fact_ids

    async def test_default_excludes_other_session_even_when_only_match(
        self, fact_store_at_run_a: FactStore,
    ) -> None:
        await fact_store_at_run_a.store(
            subject="alice", predicate="lives_in", object="Boston",
            source_interaction_id="ix-1", asserted_at=2000.0,
            session_id="run-b",
        )
        facts = await fact_store_at_run_a.recall(subject="alice", limit=10)
        assert facts == []

    async def test_explicit_list_returns_named_plus_legacy(
        self, fact_store_at_run_a: FactStore,
    ) -> None:
        ids = await _seed_three_session_facts(fact_store_at_run_a)
        facts = await fact_store_at_run_a.recall(
            subject="bob", limit=10, sessions=["run-b"],
        )
        fact_ids = {f.fact_id for f in facts}
        assert ids["run-b"] in fact_ids
        assert ids["legacy"] in fact_ids
        assert ids["run-a"] not in fact_ids

    async def test_star_returns_all_sessions(
        self, fact_store_at_run_a: FactStore,
    ) -> None:
        ids = await _seed_three_session_facts(fact_store_at_run_a)
        facts = await fact_store_at_run_a.recall(
            subject="bob", limit=10, sessions="*",
        )
        fact_ids = {f.fact_id for f in facts}
        assert fact_ids == set(ids.values())

    async def test_empty_list_raises_value_error(
        self, fact_store_at_run_a: FactStore,
    ) -> None:
        with pytest.raises(ValueError, match="non-empty list"):
            await fact_store_at_run_a.recall(subject="bob", sessions=[])

    async def test_include_superseded_respects_session_filter(
        self, fact_store_at_run_a: FactStore,
    ) -> None:
        """``include_superseded=True`` is the audit / debug read path —
        it must still respect the §D session filter; a foreign-session
        row staying invisible is the F-3 closer, not the supersede
        policy.
        """
        # Write a run-b row, then a run-b superseding row.
        await fact_store_at_run_a.store(
            subject="bob", predicate="lives_in", object="A",
            source_interaction_id="ix-1", asserted_at=3000.0,
            session_id="run-b",
        )
        await fact_store_at_run_a.store(
            subject="bob", predicate="lives_in", object="B",
            source_interaction_id="ix-2", asserted_at=3001.0,
            session_id="run-b",
        )
        facts = await fact_store_at_run_a.recall(
            subject="bob", include_superseded=True,
        )
        # Default (run-a + legacy) tier sees neither — both are run-b.
        assert facts == []


# ─── Legacy carve-out — dementia-test surface ───────────────


class TestLegacyCarveOutVisibleByDefault:
    """The ``legacy`` carve-out is load-bearing on the facts tier — it
    is the dementia-test primary recall surface (MT-MEMORY-005 Legs
    1 / 2 / 5).  A persona upgrading into v0.3.5 must not "forget"
    facts asserted before sessions existed.
    """

    async def test_legacy_fact_visible_under_default_recall(
        self, fact_store_at_run_a: FactStore,
    ) -> None:
        legacy_id = await fact_store_at_run_a.store(
            subject="bob", predicate="has_child_named", object="Mira",
            source_interaction_id=None, asserted_at=500.0,
            session_id="legacy",
        )
        facts = await fact_store_at_run_a.recall(subject="bob")
        assert {f.fact_id for f in facts} == {legacy_id}

    async def test_legacy_fact_visible_under_explicit_list(
        self, fact_store_at_run_a: FactStore,
    ) -> None:
        legacy_id = await fact_store_at_run_a.store(
            subject="bob", predicate="has_child_named", object="Mira",
            source_interaction_id=None, asserted_at=500.0,
            session_id="legacy",
        )
        facts = await fact_store_at_run_a.recall(
            subject="bob", sessions=["run-b"],
        )
        assert legacy_id in {f.fact_id for f in facts}


# ─── Cross-session supersede is session-scoped (PR 5 / ISSUE-0079) ──


class TestCrossSessionSupersedeIsSessionScoped:
    """The write-side F-3 closer on the facts tier.

    PR 3 closed F-3 on :meth:`FactStore.recall`; PR 5 closes the
    write-side by adding the ``session_id`` predicate to
    :func:`agents.memory._facts_supersede.apply_supersession`.  Symmetric
    latest-asserted-wins is now keyed on
    ``(agent_id, subject, predicate, session_id)`` so a fact written
    under ``run-b`` cannot retroactively contaminate ``run-a``'s view
    of its own fact (`ISSUE-0079
    <../../../docs/issues/ISSUE-0079-cross-session-supersede-not-scoped.md>`_).

    RFC 0026 §F amendment: latest-asserted-wins is per-session.  Each
    session keeps its own truth about ``bob.lives_in``; the ``legacy``
    carve-out participates in supersede so a pre-RFC row can still be
    superseded by an active-session reassertion (but not vice versa).
    """

    async def test_run_b_write_does_not_supersede_run_a_fact(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two sessions write the same ``(bob, lives_in)`` predicate.
        Post-fix the run-a row must remain live from run-a's view; pre-fix
        run-b's later write supersedes it globally and run-a sees nothing.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "memory.db")

            monkeypatch.setenv(SESSION_ID_ENV_VAR, "run-a")
            store_a = FactStore(agent_id="t", db_path=path)
            await store_a.initialize()
            try:
                await store_a.store(
                    subject="bob", predicate="lives_in", object="A",
                    source_interaction_id="ix-a", asserted_at=1000.0,
                    session_id="run-a",
                )
            finally:
                await store_a.close()

            monkeypatch.setenv(SESSION_ID_ENV_VAR, "run-b")
            store_b = FactStore(agent_id="t", db_path=path)
            await store_b.initialize()
            try:
                await store_b.store(
                    subject="bob", predicate="lives_in", object="B",
                    source_interaction_id="ix-b", asserted_at=2000.0,
                    session_id="run-b",
                )
            finally:
                await store_b.close()

            monkeypatch.setenv(SESSION_ID_ENV_VAR, "run-a")
            store_a2 = FactStore(agent_id="t", db_path=path)
            await store_a2.initialize()
            try:
                facts = await store_a2.recall(subject="bob")
                # Pre-fix: run-a row has superseded_by != NULL ⇒ recall is [].
                # Post-fix: the run-a fact is still visible from run-a.
                assert {f.object for f in facts} == {"A"}
            finally:
                await store_a2.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
