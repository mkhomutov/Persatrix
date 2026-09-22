"""
Tests for RFC 0031 Phase 2 PR 3 — session-scoped recall on the
``relationships`` tier, as ISSUE-0165 narrowed it.

PR 2 closed F-3 on episodes + notes; PR 3 extended the same §D contract
to the relationship reads.  ISSUE-0165 took the relationship *row* back
out of it: the row (trust, notes, identity) is one per pair and reads
the same from every session, so ``get_trust`` takes no ``sessions`` and
``get_all_relationships`` lists every row.  What stays session-scoped is
the interaction history — the count, recent interactions and first /
last seen on ``get_relationship_summary`` and ``get_all_relationships``
— with the same four-mode shape as
:mod:`tests.unit.python.test_episodic_session_scope`:

* ``sessions=None`` (default) → active session only, plus the always-
  visible ``legacy`` carve-out.
* ``sessions=["a", "b"]`` → named list, plus the ``legacy`` carve-out.
* ``sessions="*"`` → no filter (``SESSIONS_ALL``).
* ``sessions=[]`` → ``ValueError`` (§D guard against silent
  legacy-only collapse).

This file keeps the ``sessions`` modes and the row pins that sit beside
them; the reported seeded-peer case, the persona prompt path and the
epoch / principal guards on the row are in
:mod:`tests.unit.python.test_relationship_row_cross_session`.  Both
files pin the shared row, so a change to that rule fails tests in each.

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

    A distinct ``other_id`` per session gives three rows, each with one
    interaction in that session.  Every read lists all three rows; what
    the ``sessions`` modes decide is whose interactions each row counts.
    (The rows also carry that session as their first-seen tag, which no
    read looks at.)  Returns ``{session_id: other_id}``.
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
    """The four §D modes on ``get_all_relationships``: every row is
    listed, and ``sessions`` picks whose interactions each row counts."""

    async def test_default_counts_active_plus_legacy_only(
        self, memory_at_run_a: RelationshipMemory,
    ) -> None:
        ids = await _seed_three_session_relationships(memory_at_run_a)
        rels = await memory_at_run_a.get_all_relationships()
        counts = {r.other_participant_id: r.interaction_count for r in rels}
        # Every row is listed (ISSUE-0165) ...
        assert set(counts) == set(ids.values())
        # ... but a run-b interaction never counts under default recall
        # on a run-a tier.
        assert counts == {ids["run-a"]: 1, ids["run-b"]: 0, ids["legacy"]: 1}

    async def test_row_from_another_session_is_listed_without_history(
        self, memory_at_run_a: RelationshipMemory,
    ) -> None:
        """If the only row was first written in a non-active non-legacy
        session, the default read still lists it — the row is one per
        pair — with none of that session's interactions.  Before
        ISSUE-0165 this returned an empty list.
        """
        await memory_at_run_a.record_interaction(
            "trampolinist", "task_delegation",
            session_id="run-b",
        )
        rels = await memory_at_run_a.get_all_relationships()
        assert [(r.other_participant_id, r.interaction_count) for r in rels] == [
            ("trampolinist", 0),
        ]
        assert rels[0].last_interaction_at is None

    async def test_explicit_list_counts_named_plus_legacy(
        self, memory_at_run_a: RelationshipMemory,
    ) -> None:
        ids = await _seed_three_session_relationships(memory_at_run_a)
        rels = await memory_at_run_a.get_all_relationships(
            sessions=["run-b"],
        )
        counts = {r.other_participant_id: r.interaction_count for r in rels}
        assert counts == {ids["run-a"]: 0, ids["run-b"]: 1, ids["legacy"]: 1}

    async def test_star_counts_all_sessions(
        self, memory_at_run_a: RelationshipMemory,
    ) -> None:
        ids = await _seed_three_session_relationships(memory_at_run_a)
        rels = await memory_at_run_a.get_all_relationships(sessions="*")
        counts = {r.other_participant_id: r.interaction_count for r in rels}
        assert counts == dict.fromkeys(ids.values(), 1)

    async def test_empty_list_raises_value_error(
        self, memory_at_run_a: RelationshipMemory,
    ) -> None:
        with pytest.raises(ValueError, match="non-empty list"):
            await memory_at_run_a.get_all_relationships(sessions=[])


# ─── get_trust — one value per pair ─────────────────────────


class TestGetTrustIsPerPair:
    """:meth:`get_trust` reads the one row for the pair, with no session
    filter and no ``sessions`` argument (ISSUE-0165).

    PR 3 filtered it, so a peer whose row was first written in a
    non-active non-legacy session read as the neutral default (0.5).  A
    peer seeded from config under the boot session therefore never
    reached a channel's prompt.  There is no ``legacy`` case to pin any
    more: with no filter on the read, a ``legacy``-tagged row is read
    like any other, which is what the test below shows for ``run-b``.
    """

    async def test_row_first_written_in_another_session_is_read(
        self, memory_at_run_a: RelationshipMemory,
    ) -> None:
        # Bump peer-b's trust on a row first written under run-b.
        await memory_at_run_a.record_interaction(
            "peer-b", "task_delegation", session_id="run-b",
        )
        await memory_at_run_a.update_trust(
            "peer-b", 0.2, "ran a task",
        )
        # 0.5 default + 0.2 delta = 0.7, read from run-a.
        trust = await memory_at_run_a.get_trust("peer-b")
        assert trust == pytest.approx(0.7)


# ─── get_relationship_summary — sessions parameter ──────────


class TestGetRelationshipSummarySessionFilter:
    async def test_default_reads_the_row_but_not_foreign_history(
        self, memory_at_run_a: RelationshipMemory,
    ) -> None:
        """A row first written in another non-legacy session gives its
        trust and notes under default recall, matching :meth:`get_trust`,
        but none of that session's interactions.  PR 3 returned the "no
        relationship" summary here (ISSUE-0165).
        """
        await memory_at_run_a.record_interaction(
            "peer-b", "task_delegation", session_id="run-b",
        )
        await memory_at_run_a.update_trust("peer-b", 0.2, "ran a task")
        summary = await memory_at_run_a.get_relationship_summary("peer-b")
        assert summary.trust_score == pytest.approx(0.7)
        assert summary.notes == "ran a task"
        assert summary.interaction_count == 0
        assert summary.recent_interactions == []
        assert summary.first_interaction_at is None
        assert summary.last_interaction_at is None

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


class TestCrossRelationshipMemoryInstanceSharing:
    """Two :class:`RelationshipMemory` instances on the same DB with
    distinct active sessions share the relationship row but see only
    their own session's + legacy interactions.

    This was the canonical F-3 reproduction for the relationship
    surface; F-3 now lives on the epoch axis (``docs/memory-scope-axes.md``
    Decision 5), and the row is one per pair (ISSUE-0165).  Uses
    :class:`tempfile.TemporaryDirectory` to clean up the WAL companion
    files alongside the main ``.db`` on Windows (PR 449 carry-forward).
    """

    async def test_two_instances_share_the_row_not_the_history(
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
                # Give the row something only a shared read can return:
                # the "no relationship" summary PR 3 returned here carries
                # the default trust and no notes.
                await mem_a.update_trust(
                    "fingerprint-peer", 0.2, "ran the migration",
                )
            finally:
                await mem_a.close()

            monkeypatch.setenv(SESSION_ID_ENV_VAR, "run-b")
            mem_b = RelationshipMemory(agent_id="shared-agent", db_path=path)
            await mem_b.initialize()
            try:
                rels = await mem_b.get_all_relationships()
                summary = await mem_b.get_relationship_summary(
                    "fingerprint-peer",
                )
                # PR 3 made this empty; since ISSUE-0165 the run-a row is
                # listed, with none of run-a's interactions.
                assert [
                    (r.other_participant_id, r.interaction_count, r.trust_score)
                    for r in rels
                ] == [("fingerprint-peer", 0, pytest.approx(0.7))]
                # The row's own fields cross: run-b reads run-a's trust and
                # its trust note ...
                assert summary.trust_score == pytest.approx(0.7)
                assert summary.notes == "ran the migration"
                assert await mem_b.get_trust("fingerprint-peer") == pytest.approx(0.7)
                # ... while run-a's interactions stay in run-a.
                assert summary.interaction_count == 0
                assert summary.recent_interactions == []
                assert summary.last_interaction_at is None
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
        # ISSUE-0081 PR 3: the ``sessions.writes`` emission moved into the
        # shared ``agents.memory._salience.emit_session_write`` shim, so the
        # failure is injected at that shim's ``try_get_instruments`` lookup.
        from agents.memory import _salience as _sal_mod

        raising = _RaisingInstruments()
        monkeypatch.setattr(
            _sal_mod, "try_get_instruments", lambda: raising,
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
        # The row really committed — the list read shows every row of the
        # agent, so the default read finds it.
        rels = await memory_at_run_a.get_all_relationships()
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
    every INSERT; all three ``interactions`` SELECTs in
    :func:`get_relationship_summary` (the recent-history page, the
    ``COUNT(*)`` and the ``MIN(created_at)`` first-interaction-at lookup)
    now carry the §D predicate.  ``interaction_count`` is derived at read time from the
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
