"""
Symmetric latest-asserted-wins tests for
:class:`agents.memory.facts.FactStore` (RFC 0026 PR 5a).

Split out of :mod:`tests.unit.python.test_fact_store_invariants` once the
PR 5a follow-up scope pushed that file past the 500-line review-friendly
cap (see ``scripts/checks/file_size.py``).  These cases pin the
``FactStore.store`` supersede-on-insert chain rule shipped by
:mod:`agents.memory._facts_supersede`:

* Existing live rows with ``asserted_at <= new.asserted_at`` for the
  same ``(agent_id, subject, predicate)`` key are marked superseded by
  the new row.
* A strictly-newer live row dominates the new row: the new row is
  self-superseded against it.
* Equal-timestamp ties break in favour of the later arrival.

The companion :mod:`test_fact_store_invariants` continues to pin
input-validation guards, the ``supersede`` helper return contract, the
``prune`` retention primitive, ``delete_by_subject`` overlap + ACL, and
the ``Fact`` dataclass immutability contract.  The split is concern-
based: this file owns "what the symmetric retraction policy looks like
under the various write orderings" while the invariants file owns
"static guards every code path must satisfy."
"""

from __future__ import annotations

import pytest

from agents.memory.facts import FactStore

# ─── Fixture ────────────────────────────────────────────────


@pytest.fixture
async def fact_store():
    """FactStore against an in-memory SQLite DB.

    Mirrors the fixture in :mod:`tests.unit.python.test_fact_store` so
    behaviour is identical across the test split.
    """
    store = FactStore(agent_id="test-agent", db_path=":memory:")
    await store.initialize()
    yield store
    await store.close()


# ─── Symmetric latest-asserted-wins (PR 5a — RFC 0026 §F) ───


class TestSymmetricLatestAssertedWins:
    """Pin the symmetric latest-asserted-wins semantics shipped by PR 5a.

    The storage primitive's supersede-on-insert path uses
    ``asserted_at <= ?`` plus a "find newer live row" forward pass:

    * Existing live rows for the same ``(agent_id, subject, predicate)``
      with ``asserted_at`` less than or equal to the new write are
      marked superseded by the new row.
    * If a strictly-newer live row already exists for the same key,
      the new row is itself marked superseded by that newer row.

    The net effect: regardless of insert order or timestamp ties, only
    the row with the greatest ``asserted_at`` stays live; if two rows
    share the greatest timestamp the later arrival dominates.

    Replaces the earlier strict-less-than precondition (PR #339 review
    F-3); the resolution comes from PR 5a's "From PR 1 review" follow-up
    list in :doc:`docs/rfcs/0026-pr-plan.md <../../../docs/rfcs/0026-pr-plan>`.
    """

    async def test_chronological_writes_supersede_older(
        self, fact_store: FactStore,
    ):
        """Regression: forward-chronological writes still supersede."""
        old_id = await fact_store.store(
            subject="bob",
            predicate="prefers",
            object="coffee",
            source_interaction_id="ix-old",
            asserted_at=1000.0,
        )
        new_id = await fact_store.store(
            subject="bob",
            predicate="prefers",
            object="tea",
            source_interaction_id="ix-new",
            asserted_at=2000.0,
        )
        rows = await fact_store.recall(subject="bob", include_superseded=True)
        by_id = {r.fact_id: r for r in rows}
        assert by_id[old_id].superseded_by == new_id
        assert by_id[new_id].superseded_by is None
        live = await fact_store.recall(subject="bob")
        assert [r.object for r in live] == ["tea"]

    async def test_older_arrival_is_self_superseded_by_existing_newer_row(
        self, fact_store: FactStore,
    ):
        """An out-of-order older write self-supersedes against an existing newer live row."""
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
        rows = await fact_store.recall(subject="bob", include_superseded=True)
        by_id = {r.fact_id: r for r in rows}
        assert by_id[new_id].superseded_by is None
        assert by_id[old_id].superseded_by == new_id
        live = await fact_store.recall(subject="bob")
        assert [r.object for r in live] == ["tea"]

    async def test_equal_asserted_at_newer_arrival_wins(
        self, fact_store: FactStore,
    ):
        """Equal timestamps: the later arrival dominates."""
        first_id = await fact_store.store(
            subject="bob",
            predicate="prefers",
            object="tea",
            source_interaction_id="ix-a",
            asserted_at=1000.0,
        )
        second_id = await fact_store.store(
            subject="bob",
            predicate="prefers",
            object="coffee",
            source_interaction_id="ix-b",
            asserted_at=1000.0,
        )
        rows = await fact_store.recall(subject="bob", include_superseded=True)
        by_id = {r.fact_id: r for r in rows}
        assert by_id[first_id].superseded_by == second_id
        assert by_id[second_id].superseded_by is None
        live = await fact_store.recall(subject="bob")
        assert [r.object for r in live] == ["coffee"]

    async def test_out_of_order_three_writes_leave_only_newest_live(
        self, fact_store: FactStore,
    ):
        """Insert A(1000), C(3000), then B(2000) out of order.

        Trace:

        * A(1000) lands first — no candidates, A is live.
        * C(3000) lands — older SELECT finds A; A is marked superseded
          by C.  Forward-pass finds no newer live row; C is live.
        * B(2000) lands — older SELECT finds nothing live with
          ``asserted_at <= 2000`` (A is already superseded).
          Forward-pass finds C as a strictly-newer live row; B is
          marked superseded by C.

        End state: only C is live; both A and B point at C.
        """
        a_id = await fact_store.store(
            subject="bob",
            predicate="prefers",
            object="coffee",
            source_interaction_id="ix-a",
            asserted_at=1000.0,
        )
        c_id = await fact_store.store(
            subject="bob",
            predicate="prefers",
            object="water",
            source_interaction_id="ix-c",
            asserted_at=3000.0,
        )
        b_id = await fact_store.store(
            subject="bob",
            predicate="prefers",
            object="tea",
            source_interaction_id="ix-b",
            asserted_at=2000.0,
        )
        rows = await fact_store.recall(subject="bob", include_superseded=True)
        by_id = {r.fact_id: r for r in rows}
        assert by_id[a_id].superseded_by == c_id
        assert by_id[b_id].superseded_by == c_id
        assert by_id[c_id].superseded_by is None
        live = await fact_store.recall(subject="bob")
        assert [r.object for r in live] == ["water"]

    async def test_different_predicate_unaffected_by_supersession(
        self, fact_store: FactStore,
    ):
        """Symmetric latest-wins must not cross the ``(subject, predicate)`` key."""
        prefers_id = await fact_store.store(
            subject="bob",
            predicate="prefers",
            object="tea",
            source_interaction_id="ix-1",
            asserted_at=1000.0,
        )
        has_name_id = await fact_store.store(
            subject="bob",
            predicate="has_name",
            object="Bob",
            source_interaction_id="ix-2",
            asserted_at=500.0,  # earlier timestamp on a different predicate
        )
        rows = await fact_store.recall(subject="bob", include_superseded=True)
        by_id = {r.fact_id: r for r in rows}
        assert by_id[prefers_id].superseded_by is None
        assert by_id[has_name_id].superseded_by is None

    async def test_supersede_only_within_same_agent(
        self, fact_store: FactStore,
    ):
        """Per-agent ACL — RFC 0008 §H.

        A new write on agent A must not touch agent B's live rows even
        when the timestamps would otherwise trigger supersession.
        Load-bearing for tenant isolation under shared SQLite
        connection deployments.
        """
        other = FactStore(
            agent_id="other-agent",
            db_path=fact_store._db_path,
            shared_db=fact_store._ensure_db(),
        )
        other_id = await other.store(
            subject="bob",
            predicate="prefers",
            object="coffee",
            source_interaction_id="ix-other",
            asserted_at=1000.0,
        )
        # Our write at a later timestamp does NOT supersede the other
        # agent's row.
        own_id = await fact_store.store(
            subject="bob",
            predicate="prefers",
            object="tea",
            source_interaction_id="ix-mine",
            asserted_at=2000.0,
        )
        # The other agent's fact is still live.
        other_rows = await other.recall(subject="bob", include_superseded=True)
        assert len(other_rows) == 1
        assert other_rows[0].fact_id == other_id
        assert other_rows[0].superseded_by is None
        # Our own fact is live too.
        own_rows = await fact_store.recall(subject="bob")
        assert len(own_rows) == 1
        assert own_rows[0].fact_id == own_id


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
