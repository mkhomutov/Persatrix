"""
Invariant tests for :class:`agents.memory.facts.FactStore` (RFC 0026 PR 1
review follow-ups).

Split out of :mod:`tests.unit.python.test_fact_store` so each file stays
under the 500-line review-friendly cap (see ``scripts/checks/file_size.py``).
The companion file covers the happy-path CRUD / supersede chain / session
id / ``delete_by_subject`` / predicate-validation seam / per-agent
isolation contracts.  This file pins the **invariants** the storage
primitive enforces that no happy-path test exercises:

* ``TestInputValidation`` — guards on :meth:`FactStore.store` and
  :meth:`FactStore.recall` (empty subject, certainty range, limit floor
  + clamp ceiling).  Backfills coverage flagged by PR #339 review F-5.
* ``TestSupersedeHelper`` — return-value contract of the standalone
  :meth:`FactStore.supersede` helper (``True`` iff a row was updated;
  no-op on missing / already-superseded / cross-agent targets).
  Backfills coverage flagged by PR #339 review F-5.
* ``TestPrune`` — :meth:`FactStore.prune` retention primitive: live
  rows are never silently dropped, and the per-agent ACL is honoured.
  Backfills coverage flagged by PR #339 review F-5.
* ``TestAssertedAtMonotonicity`` — pins the current
  latest-asserted-wins semantics so a future change to the strict
  less-than SELECT predicate surfaces as a deliberate test update,
  not a silent semantic shift.  Documents the precondition flagged
  by PR #339 review F-3; the symmetric latest-wins variant lives on
  the PR 5 ("From PR 1 review") follow-up list.
* ``TestFactImmutability`` — pins ``@dataclass(frozen=True)`` on
  :class:`Fact` per RFC 0026 §A.  Closes the spec-drift item flagged
  by PR #339 review F-4.
"""

from __future__ import annotations

import pytest

from agents.memory.facts import Fact, FactStore


# ─── Fixture ────────────────────────────────────────────────


@pytest.fixture
async def fact_store():
    """FactStore against an in-memory SQLite DB.

    Mirrors the fixture in :mod:`tests.unit.python.test_fact_store` so
    behaviour is identical across the split — same ``agent_id``, same
    initialise-then-yield-then-close lifecycle.
    """
    store = FactStore(agent_id="test-agent", db_path=":memory:")
    await store.initialize()
    yield store
    await store.close()


# ─── delete_by_subject overlap + ACL (PR 339 review F-2 / F-5) ─


class TestDeleteBySubjectInvariants:
    """Extends :class:`tests.unit.python.test_fact_store.TestDeleteBySubject`
    with the two contracts the happy-path tests do not pin:

    * **Overlap row counts once** (F-2) — a row whose ``subject`` and
      ``source_interaction_id`` are the same string is removed by the
      first DELETE pass; the second pass sees no match.  The bucket
      semantics are per-row attribution biased toward ``by_subject``,
      not per-column match counts.
    * **Per-agent ACL** (F-5) — agent A erasing ``subject='alice'``
      must not touch agent B's ``subject='alice'`` row even when both
      stores share the same SQLite connection.  Load-bearing for
      tenant isolation under RFC 0013 traversal.
    """

    async def test_overlap_row_counts_once_in_by_subject_bucket(
        self, fact_store: FactStore,
    ):
        await fact_store.store(
            subject="alice",
            predicate="has_name",
            object="Alice",
            source_interaction_id="alice",  # Same string as subject.
            asserted_at=1000.0,
        )
        result = await fact_store.delete_by_subject("alice")
        assert result == {
            "facts_deleted_by_subject": 1,
            "facts_deleted_by_source_interaction": 0,
        }
        assert await fact_store.recall(subject="alice") == []

    async def test_scoped_to_own_agent(self, fact_store: FactStore):
        other = FactStore(
            agent_id="other-agent",
            db_path=fact_store._db_path,
            shared_db=fact_store._ensure_db(),
        )
        await other.store(
            subject="alice",
            predicate="has_name",
            object="Other-Alice",
            source_interaction_id="ix-other",
            asserted_at=1000.0,
        )
        await fact_store.store(
            subject="alice",
            predicate="has_name",
            object="Mine-Alice",
            source_interaction_id="ix-mine",
            asserted_at=1000.0,
        )

        result = await fact_store.delete_by_subject("alice")
        assert result["facts_deleted_by_subject"] == 1
        assert result["facts_deleted_by_source_interaction"] == 0

        # Local store's row is gone; the other agent's row is intact.
        assert await fact_store.recall(subject="alice") == []
        remaining = await other.recall(subject="alice")
        assert len(remaining) == 1
        assert remaining[0].object == "Other-Alice"


# ─── Input validation (PR 339 review F-5) ───────────────────


class TestInputValidation:
    """Pin the explicit ``raise`` guards on :meth:`FactStore.store`
    and :meth:`FactStore.recall` so a future refactor does not silently
    drop a validation branch.  These guards are not exercised by the
    happy-path CRUD tests; the values used there always satisfy them.
    """

    async def test_store_rejects_empty_subject(self, fact_store: FactStore):
        with pytest.raises(ValueError, match="subject must not be empty"):
            await fact_store.store(
                subject="",
                predicate="has_name",
                object="x",
                source_interaction_id="ix-1",
                asserted_at=1000.0,
            )

    async def test_store_rejects_whitespace_subject(self, fact_store: FactStore):
        with pytest.raises(ValueError, match="subject must not be empty"):
            await fact_store.store(
                subject="   ",
                predicate="has_name",
                object="x",
                source_interaction_id="ix-1",
                asserted_at=1000.0,
            )

    async def test_store_rejects_certainty_above_one(self, fact_store: FactStore):
        with pytest.raises(ValueError, match=r"certainty must be in \[0\.0, 1\.0\]"):
            await fact_store.store(
                subject="bob",
                predicate="has_name",
                object="Bob",
                source_interaction_id="ix-1",
                asserted_at=1000.0,
                certainty=1.5,
            )

    async def test_store_rejects_certainty_below_zero(self, fact_store: FactStore):
        with pytest.raises(ValueError, match=r"certainty must be in \[0\.0, 1\.0\]"):
            await fact_store.store(
                subject="bob",
                predicate="has_name",
                object="Bob",
                source_interaction_id="ix-1",
                asserted_at=1000.0,
                certainty=-0.1,
            )

    async def test_recall_rejects_zero_limit(self, fact_store: FactStore):
        with pytest.raises(ValueError, match="limit must be >= 1"):
            await fact_store.recall(subject="bob", limit=0)

    async def test_recall_clamps_limit_above_max(self):
        """``limit`` above ``_MAX_RECALL_LIMIT`` clamps to 100, not raises.

        Mirrors the :mod:`agents.memory.notes` ceiling.  The persona
        runtime cannot pull an unbounded result set into a single prompt
        even if a caller passes a pathological ``limit`` value.
        """
        # 105 distinct ``(subject, predicate)`` tuples are needed to
        # prove the clamp; the RFC 0026 §B allowlist is ~22 verbs, so
        # this test injects a permissive validator to manufacture the
        # row count without coupling to the closed vocabulary.  The
        # default-validator path is exercised by
        # :class:`tests.unit.python.test_fact_store.TestPredicateValidation`.
        store = FactStore(
            agent_id="test-agent",
            db_path=":memory:",
            predicate_validator=lambda p: None,
        )
        await store.initialize()
        try:
            for i in range(105):
                await store.store(
                    subject="bob",
                    predicate=f"p_{i}",
                    object="v",
                    source_interaction_id=f"ix-{i}",
                    asserted_at=1000.0 + i,
                )
            rows = await store.recall(subject="bob", limit=10_000)
            assert len(rows) == 100
        finally:
            await store.close()


# ─── supersede() helper contract (PR 339 review F-5) ────────


class TestSupersedeHelper:
    """The :meth:`FactStore.supersede` standalone helper exists for
    callers that need to retract a fact without writing a successor of
    identical ``(subject, predicate)`` — PR 4 retraction policy + the
    future RFC 0027 consolidation pass.  Its return-value contract
    (``True`` iff a row was updated) is exercised here so a refactor
    that breaks the no-op branches surfaces immediately.
    """

    async def test_returns_false_on_missing_fact(self, fact_store: FactStore):
        assert await fact_store.supersede("does-not-exist", "also-missing") is False

    async def test_returns_false_when_already_superseded(
        self, fact_store: FactStore,
    ):
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
        # ``store`` already supersedes ``old_id``; calling supersede()
        # again must no-op.
        assert await fact_store.supersede(old_id, new_id) is False

    async def test_does_not_cross_agent_boundary(self, fact_store: FactStore):
        """Per-agent ACL — RFC 0008 §H.

        Agent A cannot supersede agent B's fact even when both stores
        share the same SQLite connection.  Load-bearing for tenant
        isolation under shared-pool deployments.
        """
        other = FactStore(
            agent_id="other-agent",
            db_path=fact_store._db_path,
            shared_db=fact_store._ensure_db(),
        )
        other_fact = await other.store(
            subject="bob",
            predicate="prefers",
            object="tea",
            source_interaction_id="ix-other",
            asserted_at=1000.0,
        )
        assert await fact_store.supersede(other_fact, "whatever") is False
        rows = await other.recall(subject="bob", include_superseded=True)
        assert rows[0].superseded_by is None


# ─── prune() retention primitive (PR 339 review F-5) ────────


class TestPrune:
    """:meth:`FactStore.prune` is the operator-side retention primitive
    (RFC 0008 §G eviction).  Two invariants are load-bearing and not
    covered by the happy-path tests: live rows are never silently
    dropped, and the per-agent ACL is honoured.
    """

    async def test_does_not_delete_live_rows(self, fact_store: FactStore):
        await fact_store.store(
            subject="bob",
            predicate="has_name",
            object="Bob",
            source_interaction_id="ix-1",
            asserted_at=1000.0,
        )
        deleted = await fact_store.prune(before=9_999.0)
        assert deleted == 0
        rows = await fact_store.recall(subject="bob")
        assert len(rows) == 1

    async def test_deletes_only_superseded_older_than_cutoff(
        self, fact_store: FactStore,
    ):
        # Older row gets superseded by the newer write.
        await fact_store.store(
            subject="bob",
            predicate="prefers",
            object="coffee",
            source_interaction_id="ix-1",
            asserted_at=1000.0,
        )
        await fact_store.store(
            subject="bob",
            predicate="prefers",
            object="tea",
            source_interaction_id="ix-2",
            asserted_at=2000.0,
        )
        # Cutoff between the two — only the older (superseded) row
        # qualifies for prune.
        deleted = await fact_store.prune(before=1_500.0)
        assert deleted == 1
        # The live row is still there.
        rows = await fact_store.recall(subject="bob")
        assert len(rows) == 1
        assert rows[0].object == "tea"

    async def test_scoped_to_own_agent(self, fact_store: FactStore):
        other = FactStore(
            agent_id="other-agent",
            db_path=fact_store._db_path,
            shared_db=fact_store._ensure_db(),
        )
        # Create a superseded row in the other agent's store.
        await other.store(
            subject="bob",
            predicate="prefers",
            object="coffee",
            source_interaction_id="ix-1",
            asserted_at=1000.0,
        )
        await other.store(
            subject="bob",
            predicate="prefers",
            object="tea",
            source_interaction_id="ix-2",
            asserted_at=2000.0,
        )
        # Local store has nothing — prune must not touch the other agent.
        deleted = await fact_store.prune(before=9_999.0)
        assert deleted == 0
        # Verify the other agent's superseded row is still present.
        all_rows = await other.recall(subject="bob", include_superseded=True)
        assert len(all_rows) == 2


# ─── Monotonic asserted_at precondition (PR 339 review F-3) ─


class TestAssertedAtMonotonicity:
    """Pin the current latest-asserted-wins semantics in
    :meth:`FactStore.store`.

    The storage primitive's supersede-on-insert path uses
    ``asserted_at < ?`` (strict less-than), so out-of-order or
    equal-timestamp writes leave two live rows for the same
    ``(agent_id, subject, predicate)`` key.  PR 1's callers (PR 2's
    extractor uses ``interaction.closed_at``, monotonic per-agent) do
    not exercise this path; the precondition is documented on
    :meth:`FactStore.store`.  PR 4 may revisit symmetric latest-wins
    when the retraction policy lands — tracked under the
    ``feature/v031-rfc0026-followups`` "From PR 1 review" item in
    :doc:`docs/rfcs/0026-pr-plan.md <../../../docs/rfcs/0026-pr-plan>`.

    These tests assert the *current* behaviour so a future change to
    the SELECT comparison surfaces as a deliberate test update rather
    than a silent semantic shift.
    """

    async def test_older_asserted_at_does_not_supersede_newer_live_row(
        self, fact_store: FactStore,
    ):
        new_id = await fact_store.store(
            subject="bob",
            predicate="prefers",
            object="tea",
            source_interaction_id="ix-new",
            asserted_at=2000.0,
        )
        old_id = await fact_store.store(
            subject="bob",
            predicate="prefers",
            object="coffee",
            source_interaction_id="ix-old",
            asserted_at=1000.0,
        )
        # Both rows are live — the older write does not supersede the
        # newer row, and the newer row has no candidate older live row
        # to supersede (its asserted_at predates the search predicate).
        rows = await fact_store.recall(subject="bob", include_superseded=True)
        by_id = {r.fact_id: r for r in rows}
        assert by_id[new_id].superseded_by is None
        assert by_id[old_id].superseded_by is None
        live = await fact_store.recall(subject="bob")
        assert len(live) == 2

    async def test_equal_asserted_at_does_not_supersede(
        self, fact_store: FactStore,
    ):
        a_id = await fact_store.store(
            subject="bob",
            predicate="prefers",
            object="tea",
            source_interaction_id="ix-a",
            asserted_at=1000.0,
        )
        b_id = await fact_store.store(
            subject="bob",
            predicate="prefers",
            object="coffee",
            source_interaction_id="ix-b",
            asserted_at=1000.0,  # exact same timestamp
        )
        rows = await fact_store.recall(subject="bob", include_superseded=True)
        by_id = {r.fact_id: r for r in rows}
        assert by_id[a_id].superseded_by is None
        assert by_id[b_id].superseded_by is None


# ─── Fact dataclass immutability (PR 339 review F-4) ────────


class TestFactImmutability:
    """RFC 0026 §A names ``Fact`` as ``@dataclass(frozen=True)``.

    Freezing is load-bearing for the recall caller — once a fact
    instance crosses the storage boundary it represents a committed
    row; mutating it in memory would silently desynchronise the view
    from the DB.  Pin the frozen contract so a future refactor that
    drops the keyword surfaces immediately.
    """

    def test_fact_instance_is_frozen(self):
        from dataclasses import FrozenInstanceError

        fact = Fact(
            fact_id="f1",
            agent_id="a",
            subject="bob",
            predicate="has_name",
            object="Bob",
            certainty=1.0,
            source_interaction_id="ix-1",
            asserted_at=1000.0,
            last_recalled_at=None,
            superseded_by=None,
            session_id="legacy",
        )
        with pytest.raises(FrozenInstanceError):
            fact.subject = "alice"  # type: ignore[misc]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
