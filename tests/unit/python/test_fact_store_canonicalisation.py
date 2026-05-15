"""Unit tests for the PR 5c (RFC 0026 review follow-ups, slice 3) L-2
fix: :meth:`agents.memory.facts.FactStore.store` and
:meth:`FactStore.recall` canonicalise ``subject`` internally so the
storage primitive is the single source of truth for the canonical
form.

Background: the production write/read paths already canonicalise on
both sides (PR 2's extractor calls
:func:`agents.memory.fact_predicates.canonicalize_subject` before
storing; PR 3's ``_subject_seeds`` canonicalises before recall).  But
three classes of caller bypass that discipline:

1. Test fixtures (``await fact_store.store(subject="Bob", ...)``).
2. Operator-seeded facts (RFC 0026 OQ #9 deferred follow-up).
3. Future RFC 0013 erasure backfill (an ingestion path that re-asserts
   subjects from a snapshot needs the same canonicalisation).

Before PR 5c, these callers could silently write a non-canonical
subject row and miss the dementia-test recall path — defeating the
load-bearing MT-MEMORY-005 invariant.  Tightening
:meth:`FactStore.store` (and the symmetric :meth:`FactStore.recall`
read path) closes the footgun at the storage boundary.

Contract pinned: a write with a non-canonical subject is normalised
internally, and a recall using the canonical form returns the row.
"""

from __future__ import annotations

import pytest

from agents.memory.facts import FactStore


pytestmark = pytest.mark.asyncio


# ─── Fixture (mirrors test_fact_store.fact_store) ──────────


@pytest.fixture
async def fact_store():
    store = FactStore(agent_id="test-agent", db_path=":memory:")
    await store.initialize()
    try:
        yield store
    finally:
        await store.close()


# ─── L-2: FactStore.store canonicalises subject ─────────────


class TestStoreCanonicalisesSubject:
    """Pre-PR-5c, :meth:`FactStore.store` accepted ``subject`` verbatim
    (only an empty-string check at the boundary).  After PR 5c the
    storage primitive canonicalises internally so the round-trip
    works for callers that bypass the extractor's normalisation.
    """

    async def test_mixed_case_subject_recalls_under_canonical_form(
        self,
        fact_store: FactStore,
    ) -> None:
        """Caller stores ``"Bob"`` (capitalised); recall under the
        canonical form ``"bob"`` returns the row."""
        await fact_store.store(
            subject="Bob",
            predicate="prefers",
            object="tea",
            source_interaction_id="ix-1",
            asserted_at=1000.0,
        )
        results = await fact_store.recall(subject="bob")
        assert len(results) == 1
        assert results[0].predicate == "prefers"
        assert results[0].object == "tea"
        # Stored row carries the canonical form, not the input form —
        # a future joinable read (across stores, audit log replay)
        # gets a single canonical key per counterparty.
        assert results[0].subject == "bob"

    async def test_trailing_whitespace_subject_recalls_under_canonical_form(
        self,
        fact_store: FactStore,
    ) -> None:
        """Caller stores ``"bob "`` (trailing whitespace); recall
        under the canonical form ``"bob"`` returns the row."""
        await fact_store.store(
            subject="bob ",
            predicate="prefers",
            object="tea",
            source_interaction_id="ix-1",
            asserted_at=1000.0,
        )
        results = await fact_store.recall(subject="bob")
        assert len(results) == 1
        assert results[0].subject == "bob"

    async def test_internal_whitespace_subject_collapses(
        self,
        fact_store: FactStore,
    ) -> None:
        """``"Bob   Smith"`` and ``"Bob Smith"`` land on the same row
        — the multi-space form collapses via ``canonicalize_subject``
        before the INSERT."""
        await fact_store.store(
            subject="Bob   Smith",
            predicate="prefers",
            object="coffee",
            source_interaction_id="ix-1",
            asserted_at=1000.0,
        )
        results = await fact_store.recall(subject="bob smith")
        assert len(results) == 1
        assert results[0].subject == "bob smith"

    async def test_recall_canonicalises_query_subject(
        self,
        fact_store: FactStore,
    ) -> None:
        """Symmetric: a recall query with a non-canonical subject also
        canonicalises so test fixtures and direct callers do not
        need to canonicalise on the read path either."""
        await fact_store.store(
            subject="bob",
            predicate="prefers",
            object="tea",
            source_interaction_id="ix-1",
            asserted_at=1000.0,
        )
        # Non-canonical read query — must still find the row.
        results = await fact_store.recall(subject="Bob ")
        assert len(results) == 1
        assert results[0].object == "tea"

    async def test_supersede_chain_dedupes_across_canonical_form(
        self,
        fact_store: FactStore,
    ) -> None:
        """A second write with a differently-cased subject should
        supersede the first (single canonical row per
        ``(agent_id, canonical_subject, predicate)``), not write a
        second live row.  Pre-PR-5c the two writes lived under
        different ``subject`` literals and both stayed live —
        defeating the latest-asserted-wins invariant for callers
        bypassing the extractor."""
        await fact_store.store(
            subject="Bob",
            predicate="prefers",
            object="tea",
            source_interaction_id="ix-1",
            asserted_at=1000.0,
        )
        await fact_store.store(
            subject="bob",
            predicate="prefers",
            object="coffee",
            source_interaction_id="ix-2",
            asserted_at=1001.0,
        )
        live = await fact_store.recall(subject="bob")
        assert len(live) == 1, (
            f"expected single live row after canonical-form supersede; "
            f"got {[(f.subject, f.object) for f in live]}"
        )
        assert live[0].object == "coffee"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
