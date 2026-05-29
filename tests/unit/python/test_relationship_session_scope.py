"""
Tests for RFC 0031 Phase 2 PR 3 — session-scoped recall on the
``relationships`` tier.

PR 2 closed F-3 on episodes + notes; PR 3 extends the same §D contract
to the relationship reads (``get_trust`` / ``get_relationship_summary``
/ ``get_all_relationships``).  Same four-mode shape as
:mod:`tests.unit.python.test_episodic_session_scope`:

* ``sessions=None`` (default) → active session only, plus the always-
  visible ``legacy`` carve-out.
* ``sessions=["a", "b"]`` → named list, plus the ``legacy`` carve-out.
* ``sessions="*"`` → no filter (CLI / debug sentinel).
* ``sessions=[]`` → ``ValueError`` (§D guard against silent
  legacy-only collapse).

Active session is resolved once at tier construction via
:func:`agents.session_id.resolve_session_id_silent` — mirrors
:class:`agents.memory.episodic.EpisodicMemory` so the persona-direct
recall path (which bypasses :class:`agents.memory.MemoryStore`) gets the
same ``sessions=None`` contract as the facade path.

The SQL fragment shape is pinned once in
:mod:`tests.unit.python.test_session_id_session_filter`; this file
exercises the contract end-to-end through the tier public API.
"""

from __future__ import annotations

import contextlib
import os
import tempfile

import pytest

from agents.memory.relationship import RelationshipMemory
from agents.session_id import LEGACY_SESSION_ID, SESSION_ID_ENV_VAR

# ─── Helpers ────────────────────────────────────────────────


async def _seed_three_session_relationships(
    mem: RelationshipMemory,
) -> dict[str, str]:
    """Record one interaction in each of ``run-a`` / ``run-b`` / ``legacy``.

    The relationship row is first-seen tagged with the session id of
    the interaction that created it, so seeding distinct ``other_id``
    per session gives three rows tagged ``run-a`` / ``run-b`` / ``legacy``
    respectively.  Returns ``{session_id: other_id}``.
    """
    await mem.record_interaction(
        "peer-a", "task_delegation", outcome="success",
        session_id="run-a",
    )
    await mem.record_interaction(
        "peer-b", "task_delegation", outcome="success",
        session_id="run-b",
    )
    await mem.record_interaction(
        "peer-legacy", "task_delegation", outcome="success",
        session_id="legacy",
    )
    return {"run-a": "peer-a", "run-b": "peer-b", "legacy": "peer-legacy"}


@pytest.fixture
async def memory_at_run_a(monkeypatch: pytest.MonkeyPatch):
    """``RelationshipMemory`` constructed with ``PERSATRIX_SESSION_ID=run-a``."""
    monkeypatch.setenv(SESSION_ID_ENV_VAR, "run-a")
    mem = RelationshipMemory(agent_id="test-agent", db_path=":memory:")
    await mem.initialize()
    yield mem
    await mem.close()


# ─── Active-session resolution at tier construction ─────────


class TestActiveSessionResolution:
    """:class:`RelationshipMemory` resolves the active session once at __init__.

    Mirrors the :class:`EpisodicMemory` contract — tier-owned active
    session so a persona-direct caller (one that bypasses the
    :class:`MemoryStore` facade) gets the same ``sessions=None`` shape.
    """

    async def test_default_active_session_is_legacy_when_env_unset(
        self,
    ) -> None:
        mem = RelationshipMemory(agent_id="t", db_path=":memory:")
        try:
            await mem.initialize()
            assert mem._active_session_id == LEGACY_SESSION_ID
        finally:
            await mem.close()

    async def test_env_var_resolved_at_construction(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(SESSION_ID_ENV_VAR, "run-a")
        mem = RelationshipMemory(agent_id="t", db_path=":memory:")
        try:
            await mem.initialize()
            assert mem._active_session_id == "run-a"
        finally:
            await mem.close()

    async def test_active_session_id_immutable_after_construction(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(SESSION_ID_ENV_VAR, "run-a")
        mem = RelationshipMemory(agent_id="t", db_path=":memory:")
        try:
            await mem.initialize()
            monkeypatch.setenv(SESSION_ID_ENV_VAR, "run-b")
            assert mem._active_session_id == "run-a"
        finally:
            await mem.close()


# ─── get_all_relationships — sessions parameter ────────────


class TestGetAllRelationshipsSessionFilter:
    """The four §D modes on ``get_all_relationships``."""

    async def test_default_returns_active_plus_legacy_only(
        self, memory_at_run_a: RelationshipMemory,
    ) -> None:
        ids = await _seed_three_session_relationships(memory_at_run_a)
        rels = await memory_at_run_a.get_all_relationships()
        other_ids = {r.other_participant_id for r in rels}
        assert ids["run-a"] in other_ids
        assert ids["legacy"] in other_ids
        # F-3 closer assertion: a run-b row never surfaces under
        # default recall on a run-a tier.
        assert ids["run-b"] not in other_ids

    async def test_default_excludes_other_session_even_when_only_match(
        self, memory_at_run_a: RelationshipMemory,
    ) -> None:
        """If the only row is in a non-active non-legacy session, the
        default recall returns an empty list — not a fallback.  Mirrors
        the episodic "no-op filter would silently pass" guard.
        """
        await memory_at_run_a.record_interaction(
            "trampolinist", "task_delegation",
            session_id="run-b",
        )
        rels = await memory_at_run_a.get_all_relationships()
        assert rels == []

    async def test_explicit_list_returns_named_plus_legacy(
        self, memory_at_run_a: RelationshipMemory,
    ) -> None:
        ids = await _seed_three_session_relationships(memory_at_run_a)
        rels = await memory_at_run_a.get_all_relationships(
            sessions=["run-b"],
        )
        other_ids = {r.other_participant_id for r in rels}
        assert ids["run-b"] in other_ids
        assert ids["legacy"] in other_ids
        assert ids["run-a"] not in other_ids

    async def test_star_returns_all_sessions(
        self, memory_at_run_a: RelationshipMemory,
    ) -> None:
        ids = await _seed_three_session_relationships(memory_at_run_a)
        rels = await memory_at_run_a.get_all_relationships(sessions="*")
        other_ids = {r.other_participant_id for r in rels}
        assert other_ids == set(ids.values())

    async def test_empty_list_raises_value_error(
        self, memory_at_run_a: RelationshipMemory,
    ) -> None:
        with pytest.raises(ValueError, match="non-empty list"):
            await memory_at_run_a.get_all_relationships(sessions=[])


# ─── get_trust — sessions parameter ─────────────────────────


class TestGetTrustSessionFilter:
    """:meth:`get_trust` consults the §D predicate.

    A trust query for a peer in a non-active non-legacy session returns
    the neutral default (0.5) — the row is invisible from this tier's
    active session, so it must not influence trust-driven prompting.
    """

    async def test_default_excludes_other_session_row(
        self, memory_at_run_a: RelationshipMemory,
    ) -> None:
        # Bump peer-b's trust under run-b.
        await memory_at_run_a.record_interaction(
            "peer-b", "task_delegation", session_id="run-b",
        )
        await memory_at_run_a.update_trust(
            "peer-b", 0.2, "ran a task",
        )
        # Default tier recall is run-a + legacy; peer-b lives in run-b.
        trust = await memory_at_run_a.get_trust("peer-b")
        assert trust == 0.5  # neutral default — row invisible

    async def test_explicit_list_surfaces_named_session(
        self, memory_at_run_a: RelationshipMemory,
    ) -> None:
        await memory_at_run_a.record_interaction(
            "peer-b", "task_delegation", session_id="run-b",
        )
        await memory_at_run_a.update_trust(
            "peer-b", 0.2, "ran a task",
        )
        trust = await memory_at_run_a.get_trust(
            "peer-b", sessions=["run-b"],
        )
        # 0.5 default + 0.2 delta = 0.7 — the row IS visible here.
        assert trust == pytest.approx(0.7)

    async def test_legacy_carve_out_visible_by_default(
        self, memory_at_run_a: RelationshipMemory,
    ) -> None:
        await memory_at_run_a.record_interaction(
            "ancient-peer", "task_delegation", session_id="legacy",
        )
        await memory_at_run_a.update_trust(
            "ancient-peer", 0.1, "trust-bump",
        )
        trust = await memory_at_run_a.get_trust("ancient-peer")
        assert trust == pytest.approx(0.6)

    async def test_star_returns_any_session(
        self, memory_at_run_a: RelationshipMemory,
    ) -> None:
        await memory_at_run_a.record_interaction(
            "peer-b", "task_delegation", session_id="run-b",
        )
        await memory_at_run_a.update_trust(
            "peer-b", 0.2, "ran a task",
        )
        trust = await memory_at_run_a.get_trust("peer-b", sessions="*")
        assert trust == pytest.approx(0.7)

    async def test_empty_list_raises_value_error(
        self, memory_at_run_a: RelationshipMemory,
    ) -> None:
        with pytest.raises(ValueError, match="non-empty list"):
            await memory_at_run_a.get_trust("peer-a", sessions=[])


# ─── get_relationship_summary — sessions parameter ──────────


class TestGetRelationshipSummarySessionFilter:
    async def test_default_returns_empty_summary_for_foreign_session_row(
        self, memory_at_run_a: RelationshipMemory,
    ) -> None:
        """A row in another non-legacy session yields the "no relationship"
        summary under default recall, matching :meth:`get_trust`.
        """
        await memory_at_run_a.record_interaction(
            "peer-b", "task_delegation", session_id="run-b",
        )
        summary = await memory_at_run_a.get_relationship_summary("peer-b")
        # The "no row" branch returns default trust + zero interactions.
        assert summary.interaction_count == 0
        assert summary.trust_score == 0.5

    async def test_explicit_list_returns_full_summary(
        self, memory_at_run_a: RelationshipMemory,
    ) -> None:
        await memory_at_run_a.record_interaction(
            "peer-b", "task_delegation", session_id="run-b",
        )
        summary = await memory_at_run_a.get_relationship_summary(
            "peer-b", sessions=["run-b"],
        )
        assert summary.interaction_count == 1
        assert summary.other_participant_id == "peer-b"

    async def test_legacy_carve_out_visible(
        self, memory_at_run_a: RelationshipMemory,
    ) -> None:
        await memory_at_run_a.record_interaction(
            "ancient-peer", "task_delegation", session_id="legacy",
        )
        summary = await memory_at_run_a.get_relationship_summary(
            "ancient-peer",
        )
        assert summary.interaction_count == 1

    async def test_star_returns_full_summary(
        self, memory_at_run_a: RelationshipMemory,
    ) -> None:
        await memory_at_run_a.record_interaction(
            "peer-b", "task_delegation", session_id="run-b",
        )
        summary = await memory_at_run_a.get_relationship_summary(
            "peer-b", sessions="*",
        )
        assert summary.interaction_count == 1

    async def test_empty_list_raises_value_error(
        self, memory_at_run_a: RelationshipMemory,
    ) -> None:
        with pytest.raises(ValueError, match="non-empty list"):
            await memory_at_run_a.get_relationship_summary(
                "peer-a", sessions=[],
            )


# ─── Cross-tier file-share regression ──────────────────────


class TestCrossRelationshipMemoryInstanceIsolation:
    """Two :class:`RelationshipMemory` instances on the same DB with
    distinct active sessions see only their own session + legacy.

    Canonical F-3 reproduction for the relationship surface.  Uses
    :class:`tempfile.TemporaryDirectory` to clean up the WAL companion
    files alongside the main ``.db`` on Windows (PR 449 carry-forward).
    """

    async def test_two_instances_isolated_by_active_session(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "memory.db")

            monkeypatch.setenv(SESSION_ID_ENV_VAR, "run-a")
            mem_a = RelationshipMemory(agent_id="shared-agent", db_path=path)
            await mem_a.initialize()
            try:
                await mem_a.record_interaction(
                    "fingerprint-peer", "task_delegation",
                    session_id="run-a",
                )
            finally:
                await mem_a.close()

            monkeypatch.setenv(SESSION_ID_ENV_VAR, "run-b")
            mem_b = RelationshipMemory(agent_id="shared-agent", db_path=path)
            await mem_b.initialize()
            try:
                rels = await mem_b.get_all_relationships()
                # Pre-PR-3: this returned the run-a row → F-3 reproduction.
                # Post-PR-3: empty — the run-a row is invisible to run-b.
                assert rels == []
            finally:
                await mem_b.close()


# ─── F17 carry-forward — sessions_writes metric failure isolation ──


class _RaisingCounter:
    def __init__(self) -> None:
        self.calls: list[tuple[int, dict[str, object]]] = []

    def add(self, value: int, attributes: dict[str, object] | None = None) -> None:
        self.calls.append((value, dict(attributes or {})))
        raise RuntimeError("simulated OTEL backend failure (test fixture)")


class _RaisingInstruments:
    def __init__(self) -> None:
        self.sessions_writes = _RaisingCounter()


class TestRecordInteractionMetricFailureIsolated:
    """``record_interaction`` must not surface a metric-backend exception
    after ``db.commit()`` — same failure-isolation contract as
    ``EpisodicMemory.store_episode`` (M1) and
    ``NoteStore.store_note`` (PR 449).

    PR 1 second deep-review #2 (F17): the relationship-tier emit at
    :file:`agents/memory/relationship_mutations.py` was not wrapped in
    ``contextlib.suppress(Exception)``, so an OTEL backend exception
    after the row was already persisted would propagate to the caller
    as a write failure.
    """

    async def test_metric_failure_after_commit_does_not_propagate(
        self, memory_at_run_a: RelationshipMemory,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from agents.memory import relationship_mutations as _rm_mod

        raising = _RaisingInstruments()
        monkeypatch.setattr(
            _rm_mod, "try_get_instruments", lambda: raising,
        )

        # No ``pytest.raises``: the suppress wrapper must isolate the
        # metric-backend failure entirely — the row is already persisted.
        interaction_id = await memory_at_run_a.record_interaction(
            "peer-x", "task_delegation",
            session_id="run-a",
        )
        assert interaction_id, (
            "record_interaction must return a non-empty id even when the "
            "metric backend raised (commit already succeeded)"
        )
        # The row really committed — surface it via the explicit-list
        # path so the active-session filter doesn't hide it.
        rels = await memory_at_run_a.get_all_relationships(
            sessions=["run-a"],
        )
        assert any(r.other_participant_id == "peer-x" for r in rels), (
            "the relationship row was not persisted, contradicting the "
            "commit-before-metric ordering F17 assumes"
        )
        # The metric site was reached — guards against a future refactor
        # that silently removes the emit entirely.
        assert raising.sessions_writes.calls, (
            "the metric site was not reached at all; the test can no "
            "longer distinguish 'failure isolated' from 'site removed'"
        )


# ─── Interactions are session-scoped (PR 5 / ISSUE-0080) ────


class TestRecentInteractionsAreSessionScoped:
    """The read-side F-3 closer for :meth:`get_relationship_summary`'s
    secondary fetch into ``interactions``.

    Migration v10 (PR 5) added ``session_id`` to the ``interactions``
    table; :func:`record_interaction` threads the active session id onto
    every INSERT; both ``interactions`` SELECTs in
    :func:`get_relationship_summary` (the recent-history page and the
    ``MIN(created_at)`` first-interaction-at lookup) now carry the §D
    predicate.  ``interaction_count`` is derived at read time from the
    filtered ``interactions`` subquery — policy (C) in `ISSUE-0080
    <../../../docs/issues/ISSUE-0080-relationship-recent-interactions-cross-session-leak.md>`_:
    the column survives for the unfiltered admin / debug path, but the
    summary surface returns a per-session count.  Applied uniformly to
    :meth:`get_all_relationships` so cadence aggregations no longer
    inherit the cross-session-inflated count.
    """

    async def test_recent_interactions_excludes_foreign_session_history(
        self, memory_at_run_a: RelationshipMemory,
    ) -> None:
        # First-seen under run-a → relationships row tagged "run-a".
        await memory_at_run_a.record_interaction(
            "peer-a", "task_delegation", outcome="ok-A",
            session_id="run-a",
        )
        # Cross-session second interaction with the same peer.
        await memory_at_run_a.record_interaction(
            "peer-a", "task_delegation", outcome="ok-B",
            session_id="run-b",
        )
        summary = await memory_at_run_a.get_relationship_summary("peer-a")
        # Pre-fix: count=2 + both outcomes leak. Post-fix: only run-a.
        outcomes = {i.outcome for i in summary.recent_interactions}
        assert summary.interaction_count == 1, summary.interaction_count
        assert outcomes == {"ok-A"}, outcomes

    async def test_get_all_relationships_count_excludes_foreign_session(
        self, memory_at_run_a: RelationshipMemory,
    ) -> None:
        """``get_all_relationships`` reads the same leaked column —
        cadence aggregations over every visible row inherit the
        cross-session-inflated count.  Forces PR 5 to fix both list-
        and summary-mode in one migration-v10 patch.
        """
        await memory_at_run_a.record_interaction(
            "peer-a", "task_delegation", session_id="run-a",
        )
        await memory_at_run_a.record_interaction(
            "peer-a", "task_delegation", session_id="run-b",
        )
        rels = await memory_at_run_a.get_all_relationships()
        peer_a = next(r for r in rels if r.other_participant_id == "peer-a")
        # Pre-fix: count == 2.  Post-fix: count == 1.
        assert peer_a.interaction_count == 1, peer_a.interaction_count


# Keep test_session_id_metric_failure_isolation's `pytest.main` idiom.
if __name__ == "__main__":
    with contextlib.suppress(SystemExit):
        pytest.main([__file__, "-v"])
