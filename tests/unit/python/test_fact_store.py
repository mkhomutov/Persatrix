"""
Tests for :class:`agents.memory.facts.FactStore` (RFC 0026 PR 1).

PR 1 ships the storage primitive only — extractor wiring (PR 2),
``MemoryBudget`` integration (PR 3), and reinforcement / retraction
policy (PR 4) are out of scope.  The contracts asserted here are:

* CRUD (store + recall) round-trip on a fresh DB.
* Per-``(agent_id, subject, predicate)`` supersede chain on conflicting
  writes with a later ``asserted_at`` — sets ``superseded_by`` on the
  older row; recall excludes superseded rows by default.
* ``session_id`` default ``"legacy"`` matches the migration-v7 contract
  on episodes / relationships; explicit values round-trip.
* ``delete_by_subject`` traverses **both** ``subject`` and
  ``source_interaction_id`` (per RFC 0026 §H) and returns the per-column
  subtotals.  This is the GDPR-traversal primitive RFC 0013's
  ``SubjectErasure`` will wire into when it implements (target v0.5.0).
* Predicate validation is a callable injection seam — Phase 1 ships a
  permissive default; PR 2 will wire the allowlist.  The seam is
  exercised here so PR 2's swap is a one-line change with regression
  coverage.
* Recall caps + agent isolation — facts about other agents do not leak
  through ``recall(subject)`` calls scoped to the local ``agent_id``.
"""

from __future__ import annotations

import pytest

from agents.memory.facts import Fact, FactStore


# ─── Fixture ────────────────────────────────────────────────


@pytest.fixture
async def fact_store():
    """FactStore against an in-memory SQLite DB.

    Mirrors the ``memory`` fixture in ``conftest.py`` — same per-test
    isolation pattern, same ``agent_id`` convention so the test surface
    composes with the rest of the unit-test suite.
    """
    store = FactStore(agent_id="test-agent", db_path=":memory:")
    await store.initialize()
    yield store
    await store.close()


# ─── CRUD ───────────────────────────────────────────────────


class TestStoreAndRecall:
    async def test_store_returns_fact_id(self, fact_store: FactStore):
        fact_id = await fact_store.store(
            subject="bob",
            predicate="has_child_named",
            object="Mira",
            source_interaction_id="ix-1",
            asserted_at=1000.0,
        )
        assert isinstance(fact_id, str)
        assert fact_id  # non-empty

    async def test_recall_round_trip(self, fact_store: FactStore):
        await fact_store.store(
            subject="bob",
            predicate="has_child_named",
            object="Mira",
            source_interaction_id="ix-1",
            asserted_at=1000.0,
        )
        results = await fact_store.recall(subject="bob")
        assert len(results) == 1
        fact = results[0]
        assert isinstance(fact, Fact)
        assert fact.subject == "bob"
        assert fact.predicate == "has_child_named"
        assert fact.object == "Mira"
        assert fact.source_interaction_id == "ix-1"
        assert fact.asserted_at == 1000.0
        assert fact.agent_id == "test-agent"
        assert fact.superseded_by is None

    async def test_recall_returns_empty_when_no_match(
        self, fact_store: FactStore,
    ):
        await fact_store.store(
            subject="bob",
            predicate="has_name",
            object="Bob",
            source_interaction_id="ix-1",
            asserted_at=1000.0,
        )
        results = await fact_store.recall(subject="alice")
        assert results == []

    async def test_recall_respects_limit(self, fact_store: FactStore):
        # Use distinct allowlisted predicates so PR 2's default
        # validator does not reject the synthetic rows.  Five live
        # rows are needed; the predicate identity is incidental to the
        # limit-clamp invariant under test.
        predicates = [
            "has_name", "lives_in", "works_at", "has_age", "speaks_language",
        ]
        for i, pred in enumerate(predicates):
            await fact_store.store(
                subject="bob",
                predicate=pred,
                object=f"v{i}",
                source_interaction_id=f"ix-{i}",
                asserted_at=1000.0 + i,
            )
        results = await fact_store.recall(subject="bob", limit=3)
        assert len(results) == 3

    async def test_recall_default_certainty_is_one(self, fact_store: FactStore):
        await fact_store.store(
            subject="bob",
            predicate="has_name",
            object="Bob",
            source_interaction_id="ix-1",
            asserted_at=1000.0,
        )
        (fact,) = await fact_store.recall(subject="bob")
        assert fact.certainty == 1.0

    async def test_explicit_certainty_round_trips(self, fact_store: FactStore):
        await fact_store.store(
            subject="bob",
            predicate="prefers",
            object="tea",
            source_interaction_id="ix-1",
            asserted_at=1000.0,
            certainty=0.7,
        )
        (fact,) = await fact_store.recall(subject="bob")
        assert fact.certainty == pytest.approx(0.7)


# ─── Supersede chain ────────────────────────────────────────


class TestSupersedeChain:
    async def test_same_subject_predicate_later_supersedes_older(
        self, fact_store: FactStore,
    ):
        """RFC 0026 §F latest-asserted-wins retraction (storage half).

        The full retraction *policy* lands in PR 4; PR 1 ships the data
        shape so PR 4 is a recall-side filter change, not a schema bump.
        """
        old_id = await fact_store.store(
            subject="bob",
            predicate="prefers",
            object="coffee",
            source_interaction_id="ix-1",
            asserted_at=1000.0,
        )
        new_id = await fact_store.store(
            subject="bob",
            predicate="prefers",
            object="tea",
            source_interaction_id="ix-2",
            asserted_at=2000.0,
        )
        assert old_id != new_id

        # Default recall excludes superseded rows.
        results = await fact_store.recall(subject="bob")
        assert len(results) == 1
        assert results[0].object == "tea"

        # The supersede pointer is recorded on the older row.
        all_rows = await fact_store.recall(
            subject="bob", include_superseded=True,
        )
        by_id = {f.fact_id: f for f in all_rows}
        assert by_id[old_id].superseded_by == new_id
        assert by_id[new_id].superseded_by is None

    async def test_different_predicate_does_not_supersede(
        self, fact_store: FactStore,
    ):
        await fact_store.store(
            subject="bob",
            predicate="prefers",
            object="tea",
            source_interaction_id="ix-1",
            asserted_at=1000.0,
        )
        await fact_store.store(
            subject="bob",
            predicate="has_name",
            object="Bob",
            source_interaction_id="ix-2",
            asserted_at=2000.0,
        )
        results = await fact_store.recall(subject="bob")
        # Both facts are live — distinct (subject, predicate).
        assert {f.predicate for f in results} == {"prefers", "has_name"}

    async def test_supersede_only_within_same_agent(
        self, fact_store: FactStore,
    ):
        """A fact from another agent does not interact with this store's writes."""
        other = FactStore(
            agent_id="other-agent",
            db_path=fact_store._db_path,
            shared_db=fact_store._ensure_db(),
        )
        await other.store(
            subject="bob",
            predicate="prefers",
            object="coffee",
            source_interaction_id="ix-other",
            asserted_at=1000.0,
        )
        await fact_store.store(
            subject="bob",
            predicate="prefers",
            object="tea",
            source_interaction_id="ix-self",
            asserted_at=2000.0,
        )
        # The other agent's fact must not be marked superseded by this
        # agent's write — RFC 0008 §H per-agent ACL.
        other_rows = await other.recall(
            subject="bob", include_superseded=True,
        )
        assert len(other_rows) == 1
        assert other_rows[0].superseded_by is None


# ─── Session id ─────────────────────────────────────────────


class TestSessionId:
    async def test_default_session_id_is_legacy(self, fact_store: FactStore):
        await fact_store.store(
            subject="bob",
            predicate="has_name",
            object="Bob",
            source_interaction_id="ix-1",
            asserted_at=1000.0,
        )
        (fact,) = await fact_store.recall(subject="bob")
        assert fact.session_id == "legacy"

    async def test_explicit_session_id_round_trips(
        self, fact_store: FactStore,
    ):
        await fact_store.store(
            subject="bob",
            predicate="has_name",
            object="Bob",
            source_interaction_id="ix-1",
            asserted_at=1000.0,
            session_id="run-a",
        )
        (fact,) = await fact_store.recall(subject="bob")
        assert fact.session_id == "run-a"


# ─── delete_by_subject (RFC 0013 traversal primitive) ───────


class TestDeleteBySubject:
    async def test_returns_zeroed_map_when_no_match(
        self, fact_store: FactStore,
    ):
        result = await fact_store.delete_by_subject("nobody")
        assert result == {
            "facts_deleted_by_subject": 0,
            "facts_deleted_by_source_interaction": 0,
        }

    async def test_deletes_rows_where_subject_matches(
        self, fact_store: FactStore,
    ):
        await fact_store.store(
            subject="alice",
            predicate="has_name",
            object="Alice",
            source_interaction_id="ix-a",
            asserted_at=1000.0,
        )
        await fact_store.store(
            subject="alice",
            predicate="lives_in",
            object="Berlin",
            source_interaction_id="ix-b",
            asserted_at=1001.0,
        )
        await fact_store.store(
            subject="bob",
            predicate="has_name",
            object="Bob",
            source_interaction_id="ix-c",
            asserted_at=1002.0,
        )
        result = await fact_store.delete_by_subject("alice")
        assert result["facts_deleted_by_subject"] == 2
        assert result["facts_deleted_by_source_interaction"] == 0

        # alice facts gone; bob untouched.
        assert await fact_store.recall(subject="alice") == []
        assert len(await fact_store.recall(subject="bob")) == 1

    async def test_traverses_source_interaction_id_for_facts_about_others(
        self, fact_store: FactStore,
    ):
        """RFC 0026 §H — facts extracted *during* an erased subject's
        interaction are erasable, even if the declared subject is someone
        else. This is the GDPR contract: erasing Alice deletes facts
        about Bob that were derived from an interaction Alice was part of.
        """
        # Fact about Bob, but extracted during Alice's interaction.
        await fact_store.store(
            subject="bob",
            predicate="prefers",
            object="tea",
            source_interaction_id="alice-ix-1",
            asserted_at=1000.0,
        )
        # Fact about Bob from a different interaction (not Alice's).
        await fact_store.store(
            subject="bob",
            predicate="has_name",
            object="Bob",
            source_interaction_id="charlie-ix-1",
            asserted_at=1001.0,
        )
        result = await fact_store.delete_by_subject("alice-ix-1")
        assert result["facts_deleted_by_subject"] == 0
        assert result["facts_deleted_by_source_interaction"] == 1

        remaining = await fact_store.recall(subject="bob")
        assert len(remaining) == 1
        assert remaining[0].source_interaction_id == "charlie-ix-1"

    async def test_subtotals_are_disjoint_row_counts(self, fact_store: FactStore):
        """Each row contributes to exactly one bucket.

        Two **distinct** rows — one whose ``subject`` matches and one
        whose ``source_interaction_id`` matches — produce subtotals of
        ``(1, 1)``.  The bucket split exists so the audit log can show
        whether erasure landed via the declared subject traversal or
        via the reverse-edge ``source_interaction_id`` traversal; the
        per-row "counted once" semantics is pinned separately by
        :meth:`test_overlap_row_counts_once_in_by_subject_bucket`.
        """
        # Row where the subject matches.
        await fact_store.store(
            subject="alice",
            predicate="has_name",
            object="Alice",
            source_interaction_id="other-ix-1",
            asserted_at=1000.0,
        )
        # Distinct row where source_interaction_id matches.
        await fact_store.store(
            subject="bob",
            predicate="prefers",
            object="tea",
            source_interaction_id="alice",  # same string as subject above
            asserted_at=1001.0,
        )
        result = await fact_store.delete_by_subject("alice")
        assert result == {
            "facts_deleted_by_subject": 1,
            "facts_deleted_by_source_interaction": 1,
        }
        assert await fact_store.recall(subject="alice") == []
        assert await fact_store.recall(subject="bob") == []

    # Two further ``delete_by_subject`` contracts — overlap-row-counts-once
    # (F-2) and per-agent ACL (F-5) — live alongside the rest of the PR #339
    # review follow-up invariants in
    # :mod:`tests.unit.python.test_fact_store_invariants` so this file stays
    # under the 500-line review-friendly cap.


# ─── Predicate-validation seam (PR 2 wires the allowlist) ───


class TestPredicateValidation:
    async def test_default_validator_enforces_allowlist(
        self, fact_store: FactStore,
    ):
        """PR 2 swapped the Phase-1 permissive default for the RFC 0026
        §B allowlist.  An unknown predicate is rejected at the storage
        boundary so prompt-injection cannot widen the vocabulary.
        """
        with pytest.raises(ValueError, match="not in allowlist"):
            await fact_store.store(
                subject="bob",
                predicate="arbitrary_phase1_predicate",
                object="v",
                source_interaction_id="ix-1",
                asserted_at=1000.0,
            )

    async def test_default_validator_accepts_allowlisted(
        self, fact_store: FactStore,
    ):
        """Sanity check the swap did not over-rotate — an allowlisted
        verb still stores successfully under the PR 2 default."""
        fact_id = await fact_store.store(
            subject="bob",
            predicate="has_name",
            object="Bob",
            source_interaction_id="ix-1",
            asserted_at=1000.0,
        )
        assert fact_id

    async def test_custom_validator_can_reject(self):
        """A caller can inject a stricter validator that raises."""
        def only_allow_has_name(predicate: str) -> None:
            if predicate != "has_name":
                raise ValueError(f"rejected: {predicate}")

        store = FactStore(
            agent_id="test-agent",
            db_path=":memory:",
            predicate_validator=only_allow_has_name,
        )
        await store.initialize()
        try:
            with pytest.raises(ValueError, match="rejected: prefers"):
                await store.store(
                    subject="bob",
                    predicate="prefers",
                    object="tea",
                    source_interaction_id="ix-1",
                    asserted_at=1000.0,
                )
        finally:
            await store.close()


# ─── Agent isolation ────────────────────────────────────────


class TestAgentIsolation:
    async def test_recall_scoped_to_own_agent_id(
        self, fact_store: FactStore,
    ):
        other = FactStore(
            agent_id="other-agent",
            db_path=fact_store._db_path,
            shared_db=fact_store._ensure_db(),
        )
        await other.store(
            subject="bob",
            predicate="has_name",
            object="Other-Bob",
            source_interaction_id="ix-other",
            asserted_at=1000.0,
        )
        await fact_store.store(
            subject="bob",
            predicate="has_name",
            object="Mine-Bob",
            source_interaction_id="ix-mine",
            asserted_at=1000.0,
        )
        own_results = await fact_store.recall(subject="bob")
        assert len(own_results) == 1
        assert own_results[0].object == "Mine-Bob"

        other_results = await other.recall(subject="bob")
        assert len(other_results) == 1
        assert other_results[0].object == "Other-Bob"


# Storage-primitive invariant tests (PR #339 review follow-ups
# F-3 / F-4 / F-5) live in
# :mod:`tests.unit.python.test_fact_store_invariants` so each test file
# stays under the 500-line review-friendly cap.  Edits to the F-3 / F-4 /
# F-5 contracts belong in that file.


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
