"""Unit tests for :meth:`agents.memory.facts.FactStore.mark_recalled`
(RFC 0026 PR 4 — use-based reinforcement).

PR 4 introduces the ``last_recalled_at`` write that fires whenever the
:class:`~agents.persona_runtime.memory_budget.MemoryBudget` allocator
admits a fact into the persona's working-memory ``facts_context``
section.  The write composes with :doc:`RFC 0008 §G
<../../../docs/rfcs/0008-agent-memory-context-optimization>` decay /
validation via the same scoring seam — the calibration formula lands
in :doc:`RFC 0008 calibration review
<../../../docs/rfcs/0008-calibration-review>`; this storage primitive
ships only the write.

Contracts pinned here:

* ``mark_recalled`` sets ``last_recalled_at`` on every named fact_id
  belonging to this agent.
* The write does **not** alter ``asserted_at``, ``certainty``, or
  any other column — reinforcement is orthogonal to retraction.
* The write is **idempotent**: calling twice with the same
  ``fact_ids`` produces the same final state (the second call
  overwrites with the new timestamp).
* The per-agent ACL is honoured — calling ``mark_recalled`` on another
  agent's fact_id silently skips that row (no cross-tenant writes).
* Empty / missing fact_ids are no-ops (no error raised).
* The ``at`` argument defaults to :func:`time.time` at call time.
* ``last_recalled_at`` is **monotone non-decreasing**: an older ``at``
  value never clobbers a newer one (PR #342 review N-1).  The decay /
  validation seam in :doc:`RFC 0008 §G
  <../../../docs/rfcs/0008-agent-memory-context-optimization>`
  composes with this column on a "newest recall wins" model, so a
  backwards step (NTP step-back, operator-supplied ``at`` from an
  older interaction) must not silently age the fact out by resetting
  the column to a stale timestamp.
"""

from __future__ import annotations

import time

import pytest

from agents.memory.facts import FactStore


@pytest.fixture
async def fact_store():
    store = FactStore(agent_id="test-agent", db_path=":memory:")
    await store.initialize()
    yield store
    await store.close()


class TestMarkRecalled:
    async def test_writes_last_recalled_at_on_admitted_fact(
        self, fact_store: FactStore,
    ) -> None:
        fact_id = await fact_store.store(
            subject="bob",
            predicate="prefers",
            object="tea",
            source_interaction_id="i1",
            asserted_at=1000.0,
        )
        rows_before = await fact_store.recall(subject="bob")
        assert rows_before[0].last_recalled_at is None

        await fact_store.mark_recalled([fact_id], at=2500.0)

        rows_after = await fact_store.recall(subject="bob")
        assert rows_after[0].last_recalled_at == 2500.0

    async def test_default_at_is_now(self, fact_store: FactStore) -> None:
        fact_id = await fact_store.store(
            subject="bob",
            predicate="prefers",
            object="tea",
            source_interaction_id="i1",
            asserted_at=1000.0,
        )
        before = time.time()
        await fact_store.mark_recalled([fact_id])
        after = time.time()
        rows = await fact_store.recall(subject="bob")
        assert rows[0].last_recalled_at is not None
        assert before <= rows[0].last_recalled_at <= after

    async def test_idempotent(self, fact_store: FactStore) -> None:
        fact_id = await fact_store.store(
            subject="bob",
            predicate="prefers",
            object="tea",
            source_interaction_id="i1",
            asserted_at=1000.0,
        )
        await fact_store.mark_recalled([fact_id], at=2500.0)
        await fact_store.mark_recalled([fact_id], at=2500.0)
        rows = await fact_store.recall(subject="bob")
        assert rows[0].last_recalled_at == 2500.0

    async def test_later_call_overwrites_timestamp(
        self, fact_store: FactStore,
    ) -> None:
        fact_id = await fact_store.store(
            subject="bob",
            predicate="prefers",
            object="tea",
            source_interaction_id="i1",
            asserted_at=1000.0,
        )
        await fact_store.mark_recalled([fact_id], at=2500.0)
        await fact_store.mark_recalled([fact_id], at=3000.0)
        rows = await fact_store.recall(subject="bob")
        assert rows[0].last_recalled_at == 3000.0

    async def test_older_at_does_not_clobber_newer(
        self, fact_store: FactStore,
    ) -> None:
        """Monotone non-decreasing contract (PR #342 review N-1).

        ``last_recalled_at`` composes with RFC 0008 §G decay on a
        "newest recall wins" basis.  Calling :meth:`mark_recalled`
        with an older ``at`` than the column's current value would
        silently age the fact out under that model, so the UPDATE
        clamps to ``MAX(existing, supplied)``.  In production
        ``time.time()`` is monotonic per-process and the issue is
        unreachable; the failure modes are NTP step-back, an operator
        replaying an older interaction's timestamp via the OQ #9
        seeded-facts path, and any test fixture that exercises an
        explicit ``at`` kwarg out of order.
        """
        fact_id = await fact_store.store(
            subject="bob",
            predicate="prefers",
            object="tea",
            source_interaction_id="i1",
            asserted_at=1000.0,
        )
        await fact_store.mark_recalled([fact_id], at=3000.0)
        await fact_store.mark_recalled([fact_id], at=2500.0)
        rows = await fact_store.recall(subject="bob")
        assert rows[0].last_recalled_at == 3000.0

    async def test_equal_at_is_idempotent(
        self, fact_store: FactStore,
    ) -> None:
        """The ``MAX`` guard treats equal timestamps as a no-op write
        (the existing value is also the max).  Important so the
        idempotent retry surface in :meth:`test_idempotent` survives
        the monotonicity tightening — a recall path that fires twice
        on the same turn must converge to the same column value, not
        accumulate side-effects.
        """
        fact_id = await fact_store.store(
            subject="bob",
            predicate="prefers",
            object="tea",
            source_interaction_id="i1",
            asserted_at=1000.0,
        )
        await fact_store.mark_recalled([fact_id], at=2500.0)
        await fact_store.mark_recalled([fact_id], at=2500.0)
        rows = await fact_store.recall(subject="bob")
        assert rows[0].last_recalled_at == 2500.0

    async def test_first_call_sets_from_null(
        self, fact_store: FactStore,
    ) -> None:
        """``last_recalled_at`` starts ``NULL``; the first
        :meth:`mark_recalled` must succeed regardless of how small
        ``at`` is — the ``MAX(COALESCE(existing, 0), supplied)`` shape
        means a NULL existing value collapses to 0, so any positive
        ``at`` (including small ones like ``1.0``) wins.  Regression
        guard: a naive ``MAX(existing, supplied)`` without ``COALESCE``
        would yield ``NULL`` on the first call under SQLite's NULL
        propagation rules.
        """
        fact_id = await fact_store.store(
            subject="bob",
            predicate="prefers",
            object="tea",
            source_interaction_id="i1",
            asserted_at=1000.0,
        )
        await fact_store.mark_recalled([fact_id], at=1.0)
        rows = await fact_store.recall(subject="bob")
        assert rows[0].last_recalled_at == 1.0

    async def test_does_not_touch_other_columns(
        self, fact_store: FactStore,
    ) -> None:
        fact_id = await fact_store.store(
            subject="bob",
            predicate="prefers",
            object="tea",
            source_interaction_id="i1",
            asserted_at=1000.0,
            certainty=0.75,
        )
        await fact_store.mark_recalled([fact_id], at=2500.0)
        rows = await fact_store.recall(subject="bob")
        row = rows[0]
        assert row.asserted_at == 1000.0
        assert row.certainty == 0.75
        assert row.object == "tea"
        assert row.predicate == "prefers"
        assert row.subject == "bob"
        assert row.source_interaction_id == "i1"
        assert row.superseded_by is None

    async def test_empty_list_is_noop(self, fact_store: FactStore) -> None:
        # Must not raise.
        await fact_store.mark_recalled([], at=2500.0)

    async def test_unknown_fact_id_silently_skipped(
        self, fact_store: FactStore,
    ) -> None:
        await fact_store.mark_recalled(["does-not-exist"], at=2500.0)
        # Storing a real fact afterwards still works.
        fact_id = await fact_store.store(
            subject="bob",
            predicate="prefers",
            object="tea",
            source_interaction_id="i1",
            asserted_at=1000.0,
        )
        rows = await fact_store.recall(subject="bob")
        assert rows[0].fact_id == fact_id
        assert rows[0].last_recalled_at is None

    async def test_per_agent_acl_honoured(self, fact_store: FactStore) -> None:
        """Calling ``mark_recalled`` on another agent's fact must no-op."""
        other = FactStore(
            agent_id="other-agent",
            db_path=fact_store._db_path,
            shared_db=fact_store._ensure_db(),
        )
        other_fact = await other.store(
            subject="bob",
            predicate="prefers",
            object="tea",
            source_interaction_id="i1",
            asserted_at=1000.0,
        )
        await fact_store.mark_recalled([other_fact], at=2500.0)

        rows = await other.recall(subject="bob")
        assert rows[0].last_recalled_at is None

    async def test_partial_overlap_marks_own_skips_other(
        self, fact_store: FactStore,
    ) -> None:
        """Mixed list — own fact_id gets the write, other agent's does not."""
        other = FactStore(
            agent_id="other-agent",
            db_path=fact_store._db_path,
            shared_db=fact_store._ensure_db(),
        )
        own_fact = await fact_store.store(
            subject="bob",
            predicate="prefers",
            object="tea",
            source_interaction_id="i1",
            asserted_at=1000.0,
        )
        other_fact = await other.store(
            subject="bob",
            predicate="prefers",
            object="tea",
            source_interaction_id="i2",
            asserted_at=1000.0,
        )

        await fact_store.mark_recalled([own_fact, other_fact], at=2500.0)

        own_rows = await fact_store.recall(subject="bob")
        assert own_rows[0].last_recalled_at == 2500.0

        other_rows = await other.recall(subject="bob")
        assert other_rows[0].last_recalled_at is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
