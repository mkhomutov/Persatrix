"""ISSUE-0081 residual — ``delete_by_subject`` is principal-scoped (v0.3.14 PR 3).

The erasure primitive
(:func:`agents.memory._facts_erasure.delete_by_subject`) was scoped to
``agent_id`` only, which was harmless for as long as every caller resolved
the one shared ``'local'`` principal.  v0.3.14 PR 2 fed the rail, so from
the day the orchestrator emits a verified principal one person's GDPR
erasure would delete *another person's* facts about the same subject — the
write-side mirror of the cross-tenant read the strict recall predicate
already forbids.

This file is the gate ISSUE-0081's plan-opening note names, in the PR 4
carve-out idiom: **a foreign principal's erasure call can neither count nor
delete another principal's rows.**  Both halves matter — the count is the
return value RFC 0013's ``SubjectErasure`` will audit as
``records_deleted``, so a foreign row leaking into the tally is a privacy
disclosure even when the DELETE itself is correctly scoped.

Deliberately *not* scoped here: ``session_id`` and ``epoch_id``.  A
right-to-erasure traversal must reach every row the principal ever wrote,
including rows from an earlier room or an earlier run — pinned below by
:meth:`TestErasureSpansTheOtherAxes.test_erasure_spans_sessions_and_epochs`
so a future "consistency" patch cannot narrow the traversal into a silent
GDPR miss.

Companion coverage: the per-agent ACL half lives in
:class:`tests.unit.python.test_fact_store_invariants.TestDeleteBySubjectInvariants`,
the traversal/return-shape contract in
:class:`tests.unit.python.test_fact_store.TestDeleteBySubject`.
"""

from __future__ import annotations

import pytest

from agents.epoch_id import epoch_scope
from agents.memory.facts import FactStore
from agents.principal_id import principal_scope
from agents.session_id import session_scope

_TENANT_A = "tenant-a"
_TENANT_B = "tenant-b"


@pytest.fixture
async def fact_store():
    """FactStore against an in-memory SQLite DB.

    Mirrors the fixture in :mod:`tests.unit.python.test_fact_store` so
    behaviour is identical across the split.
    """
    store = FactStore(agent_id="test-agent", db_path=":memory:")
    await store.initialize()
    yield store
    await store.close()


async def _store_fact(
    store: FactStore,
    *,
    subject: str = "alice",
    object: str,
    source_interaction_id: str = "ix-1",
) -> None:
    await store.store(
        subject=subject,
        predicate="has_name",
        object=object,
        source_interaction_id=source_interaction_id,
        asserted_at=1000.0,
    )


# ─── The gate: neither count nor delete another tenant's rows ─


class TestErasurePrincipalScope:
    """*A foreign principal's erasure call can neither count nor delete
    another principal's rows* — ISSUE-0081's stated TDD gate for the
    erasure residual, on both traversal columns."""

    async def test_foreign_principal_cannot_delete_by_subject(
        self, fact_store: FactStore,
    ) -> None:
        with principal_scope(_TENANT_A):
            await _store_fact(fact_store, object="A-Alice")

        # Tenant B erases the same subject: nothing of A's is counted…
        with principal_scope(_TENANT_B):
            result = await fact_store.delete_by_subject("alice")
        assert result == {
            "facts_deleted_by_subject": 0,
            "facts_deleted_by_source_interaction": 0,
        }

        # …and nothing of A's is deleted.
        with principal_scope(_TENANT_A):
            survivors = await fact_store.recall(subject="alice")
        assert len(survivors) == 1
        assert survivors[0].object == "A-Alice"

    async def test_foreign_principal_cannot_delete_by_source_interaction(
        self, fact_store: FactStore,
    ) -> None:
        # The reverse-edge traversal: erased by ``source_interaction_id``,
        # the column whose value is not the declared subject.
        with principal_scope(_TENANT_A):
            await _store_fact(
                fact_store,
                subject="bob",
                object="A-Bob",
                source_interaction_id="alice-ix-1",
            )

        with principal_scope(_TENANT_B):
            result = await fact_store.delete_by_subject("alice-ix-1")
        assert result["facts_deleted_by_source_interaction"] == 0

        with principal_scope(_TENANT_A):
            assert len(await fact_store.recall(subject="bob")) == 1

    async def test_owner_erasure_counts_only_its_own_rows(
        self, fact_store: FactStore,
    ) -> None:
        # Both tenants hold a fact about the same subject.
        with principal_scope(_TENANT_A):
            await _store_fact(
                fact_store, object="A-Alice", source_interaction_id="ix-a",
            )
        with principal_scope(_TENANT_B):
            await _store_fact(
                fact_store, object="B-Alice", source_interaction_id="ix-b",
            )

        with principal_scope(_TENANT_A):
            result = await fact_store.delete_by_subject("alice")

        # The count is the RFC 0013 ``records_deleted`` audit value: a
        # foreign row in the tally would disclose that another tenant
        # holds facts about this subject.
        assert result == {
            "facts_deleted_by_subject": 1,
            "facts_deleted_by_source_interaction": 0,
        }
        with principal_scope(_TENANT_A):
            assert await fact_store.recall(subject="alice") == []
        with principal_scope(_TENANT_B):
            remaining = await fact_store.recall(subject="alice")
        assert len(remaining) == 1
        assert remaining[0].object == "B-Alice"

    async def test_tenant_erasure_cannot_reach_the_local_baseline(
        self, fact_store: FactStore,
    ) -> None:
        # Pre-activation shape: migration v11 backfilled every existing
        # row to ``'local'``.  An authenticated tenant's erasure must not
        # reach it (the same partition the recall predicate draws).
        await _store_fact(fact_store, object="Baseline-Alice")

        with principal_scope(_TENANT_A):
            result = await fact_store.delete_by_subject("alice")
        assert result["facts_deleted_by_subject"] == 0

        # Positive control: the 'local' principal (no scope) does erase it
        # through the very same call, so the zero above is the principal
        # boundary — not a row that silently failed to store.
        assert len(await fact_store.recall(subject="alice")) == 1
        assert (await fact_store.delete_by_subject("alice"))[
            "facts_deleted_by_subject"
        ] == 1

    async def test_single_tenant_path_is_unchanged(
        self, fact_store: FactStore,
    ) -> None:
        # No principal scope anywhere — the CLI / single-tenant / pre-auth
        # deployment.  Every row resolves ``'local'`` on both the write and
        # the erasure path, so the predicate is a no-delta.
        await _store_fact(
            fact_store, object="Alice", source_interaction_id="alice",
        )
        assert await fact_store.delete_by_subject("alice") == {
            "facts_deleted_by_subject": 1,
            "facts_deleted_by_source_interaction": 0,
        }
        assert await fact_store.recall(subject="alice") == []


# ─── The traversal stays wide on the other two axes ──────────


class TestErasureSpansTheOtherAxes:
    """Erasure is scoped to the *tenant*, never to the room or the run.

    ``session_id`` and ``epoch_id`` are deliberately absent from the
    predicate: a right-to-erasure traversal that missed the rows a person
    wrote in an earlier room or an earlier run would be a silent GDPR
    failure, and silent is the whole hazard RFC 0026 §H ships this
    primitive to avoid.
    """

    async def test_erasure_spans_sessions_and_epochs(
        self, fact_store: FactStore,
    ) -> None:
        with principal_scope(_TENANT_A):
            with session_scope("room-1"), epoch_scope("run-1"):
                await _store_fact(
                    fact_store, object="A-1", source_interaction_id="ix-1",
                )
            with session_scope("room-2"), epoch_scope("run-2"):
                await _store_fact(
                    fact_store, object="A-2", source_interaction_id="ix-2",
                )

            # One call, from a third room and a third run, reaches both.
            with session_scope("room-3"), epoch_scope("run-3"):
                result = await fact_store.delete_by_subject("alice")

        assert result["facts_deleted_by_subject"] == 2
