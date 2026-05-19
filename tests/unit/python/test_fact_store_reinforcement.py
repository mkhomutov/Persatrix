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
* A non-empty ``mark_recalled`` call emits **one** ``fact.recalled``
  RFC 0026 §G audit record naming every requested ``fact_id`` — once
  per call, not per id, so audit volume stays bounded (PR #342
  second-pass review DR2-N-2).  An empty call emits nothing.
* The reinforcement UPDATE **chunks** its ``fact_id`` IN-list so an
  arbitrarily large id list cannot exceed SQLite's per-statement host
  parameter cap (``SQLITE_MAX_VARIABLE_NUMBER`` — 999 on pre-3.32
  builds) (PR #342 second-pass review DR2-N-3).
"""

from __future__ import annotations

import logging
import time
from typing import Any

import pytest

from agents.memory._facts_reinforce import mark_recalled_for_agent
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

    @pytest.mark.parametrize("at_value", [1.0, 0.0])
    async def test_first_call_sets_from_null(
        self, fact_store: FactStore, at_value: float,
    ) -> None:
        """``last_recalled_at`` starts ``NULL``; the first
        :meth:`mark_recalled` must succeed regardless of how small
        ``at`` is — the ``MAX(COALESCE(existing, 0), supplied)`` shape
        means a NULL existing value collapses to 0, so any
        non-negative ``at`` wins.  Regression guard: a naive
        ``MAX(existing, supplied)`` without ``COALESCE`` would yield
        ``NULL`` on the first call under SQLite's NULL propagation
        rules.

        Parameterised over ``[1.0, 0.0]`` (PR #342 third-pass review
        DR3-L-3): ``at=0.0`` is the boundary where the supplied value
        ties the ``COALESCE`` floor — the column still flips from
        ``NULL`` to ``0.0`` (a real state change), which the equality
        assertion below pins.  ``time.time()`` is never 0 in
        production; the case guards the OQ #9 operator-seeded path and
        the future RFC 0013 erasure backfill, which may supply ``at``
        from sources other than the wall clock.
        """
        fact_id = await fact_store.store(
            subject="bob",
            predicate="prefers",
            object="tea",
            source_interaction_id="i1",
            asserted_at=1000.0,
        )
        await fact_store.mark_recalled([fact_id], at=at_value)
        rows = await fact_store.recall(subject="bob")
        assert rows[0].last_recalled_at == at_value

    async def test_negative_at_is_noop_on_populated_column(
        self, fact_store: FactStore,
    ) -> None:
        """A negative ``at`` never clobbers a populated column
        (PR #342 third-pass review DR3-L-3).

        ``MAX(COALESCE(last_recalled_at, 0), -1.0)`` collapses to the
        existing value for any non-negative column state, so the call
        is a no-op.  Unreachable in production — ``time.time()`` is
        monotone non-negative per process — but the case pins the
        clamp so a future SQL evolution cannot silently regress it.
        """
        fact_id = await fact_store.store(
            subject="bob",
            predicate="prefers",
            object="tea",
            source_interaction_id="i1",
            asserted_at=1000.0,
        )
        await fact_store.mark_recalled([fact_id], at=2500.0)
        await fact_store.mark_recalled([fact_id], at=-1.0)
        rows = await fact_store.recall(subject="bob")
        assert rows[0].last_recalled_at == 2500.0

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


# ─── Audit emission (PR #342 second-pass review DR2-N-2) ─────


def _audit_records(
    caplog: pytest.LogCaptureFixture, event: str,
) -> list[logging.LogRecord]:
    """Return captured ``audit=True`` records whose event name is ``event``.

    Mirrors the dual-surface lookup in
    :mod:`tests.unit.python.test_fact_store_audit` — unit tests run
    before :func:`configure_logging` builds the structlog chain, so the
    event dict may land either as ``record.msg`` (structlog native) or
    as ``record.msg`` string + attributes (stdlib bridge).
    """
    out: list[logging.LogRecord] = []
    for rec in caplog.records:
        if isinstance(rec.msg, dict):
            if rec.msg.get("audit") is True and rec.msg.get("event") == event:
                out.append(rec)
        elif getattr(rec, "audit", None) is True and rec.msg == event:
            out.append(rec)
    return out


def _field(rec: logging.LogRecord, key: str) -> Any:
    """Read a structured field off a captured audit record (either surface)."""
    if isinstance(rec.msg, dict):
        return rec.msg.get(key)
    return getattr(rec, key, None)


class TestMarkRecalledAudit:
    """DR2-N-2 — ``mark_recalled`` emits a bounded ``fact.recalled`` audit
    record so the RFC 0026 §G audit log is not blind to reinforcement.

    ``store`` / ``supersede`` already emit ``fact.store`` / ``fact.supersede``;
    before this slice the reinforcement write left no audit signal, so an
    MT-MEMORY-005 leg-failure analysis could not tell from the audit log
    which facts were reinforced on a given turn.
    """

    async def test_emits_one_fact_recalled_record(
        self, fact_store: FactStore, caplog: pytest.LogCaptureFixture,
    ) -> None:
        fact_id = await fact_store.store(
            subject="bob",
            predicate="prefers",
            object="tea",
            source_interaction_id="i1",
            asserted_at=1000.0,
        )
        caplog.set_level(logging.INFO, logger="agents.memory.facts")
        caplog.clear()
        await fact_store.mark_recalled([fact_id], at=2500.0)

        records = _audit_records(caplog, "fact.recalled")
        assert len(records) == 1
        rec = records[0]
        assert _field(rec, "agent_id") == "test-agent"
        assert list(_field(rec, "fact_ids")) == [fact_id]
        assert _field(rec, "at") == 2500.0

    async def test_one_record_per_call_not_per_fact(
        self, fact_store: FactStore, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Audit volume is bounded — a multi-fact call emits a single
        record carrying the whole id list, not one record per id.
        """
        ids = [
            await fact_store.store(
                subject=subject,
                predicate="prefers",
                object="tea",
                source_interaction_id="i1",
                asserted_at=1000.0,
            )
            for subject in ("bob", "alice", "carol")
        ]
        caplog.set_level(logging.INFO, logger="agents.memory.facts")
        caplog.clear()
        await fact_store.mark_recalled(ids, at=2500.0)

        records = _audit_records(caplog, "fact.recalled")
        assert len(records) == 1
        assert sorted(_field(records[0], "fact_ids")) == sorted(ids)

    async def test_empty_call_emits_no_record(
        self, fact_store: FactStore, caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.INFO, logger="agents.memory.facts")
        caplog.clear()
        await fact_store.mark_recalled([], at=2500.0)
        assert _audit_records(caplog, "fact.recalled") == []


# ─── IN-list chunking (PR #342 second-pass review DR2-N-3) ───


class _ExecuteCountingConnection:
    """Connection proxy that records every ``UPDATE facts`` statement.

    Lets a test assert that :func:`mark_recalled_for_agent` splits a
    large ``fact_id`` list across multiple UPDATE statements rather than
    binding every id into one query (which would breach SQLite's
    per-statement host-parameter cap on older builds).
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.update_statements: list[str] = []

    def execute(self, sql: str, parameters: Any = ()) -> Any:
        if sql.strip().upper().startswith("UPDATE FACTS"):
            self.update_statements.append(sql)
        return self._inner.execute(sql, parameters)

    def commit(self) -> Any:
        return self._inner.commit()


class TestMarkRecalledChunking:
    """DR2-N-3 — the reinforcement UPDATE chunks its IN-list.

    Today's call site is bounded (≤40 ids) but the helper accepts an
    arbitrary iterable and is reachable from the future RFC 0013
    erasure backfill and RFC 0008 calibration paths; chunking
    future-proofs the API against ``SQLITE_MAX_VARIABLE_NUMBER``.
    """

    async def test_large_id_list_issues_multiple_updates(
        self, fact_store: FactStore,
    ) -> None:
        live_ids = [
            await fact_store.store(
                subject=subject,
                predicate="prefers",
                object="tea",
                source_interaction_id="i1",
                asserted_at=1000.0,
            )
            for subject in ("bob", "alice", "carol")
        ]
        # Pad well past the conservative pre-3.32 cap (999) so the
        # helper is forced to split into more than one statement.
        id_list = live_ids + [f"pad-{n}" for n in range(1500)]

        counting = _ExecuteCountingConnection(fact_store._ensure_db())
        await mark_recalled_for_agent(
            counting, "test-agent", id_list, at=2500.0,
        )

        assert len(counting.update_statements) >= 2
        # The real facts still received the reinforcement write.
        for subject in ("bob", "alice", "carol"):
            rows = await fact_store.recall(subject=subject)
            assert rows[0].last_recalled_at == 2500.0

    async def test_chunk_boundary_count_is_exact(
        self, fact_store: FactStore,
    ) -> None:
        """A list one id over a chunk boundary issues exactly two UPDATEs."""
        from agents.memory._facts_reinforce import _MAX_IDS_PER_UPDATE

        id_list = [f"pad-{n}" for n in range(_MAX_IDS_PER_UPDATE + 1)]
        counting = _ExecuteCountingConnection(fact_store._ensure_db())
        await mark_recalled_for_agent(
            counting, "test-agent", id_list, at=2500.0,
        )
        assert len(counting.update_statements) == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
