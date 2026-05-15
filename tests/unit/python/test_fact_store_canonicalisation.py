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

PR 5d M-1 — :meth:`FactStore.delete_by_subject` canonicalisation
----------------------------------------------------------------
PR 5c hardened ``store`` + ``recall`` but left ``delete_by_subject``
on raw input.  After 5c every persisted row carries the canonical
subject (e.g. ``"bob"``), so a caller passing ``"Bob"`` to
:meth:`delete_by_subject` would run ``DELETE … WHERE subject = 'Bob'``
and match zero rows — a silent miss indistinguishable from "no facts
about this subject."  This is exactly the GDPR / CCPA failure mode
the L-2 docstring on ``store`` names ("the future RFC 0013 erasure
backfill") and the storage primitive is meant to fence off.

PR 5d closes the gap by canonicalising the ``subject`` traversal in
:meth:`delete_by_subject`.  The ``source_interaction_id`` traversal
deliberately stays un-canonicalised — that column holds opaque UUIDs,
not subject strings, and ``canonicalize_subject`` would be a category
error (it would casefold the UUID and silently miss).
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


# ─── M-1: FactStore.delete_by_subject canonicalises subject ─


class TestDeleteBySubjectCanonicalisesSubject:
    """PR 5d M-1 — :meth:`FactStore.delete_by_subject` canonicalises
    its ``subject_id`` argument before the DELETE so an erasure call
    with a non-canonical spelling hits the row a canonical write
    persisted.

    Symmetry argument: post-PR-5c, ``store`` writes under the
    canonical form and ``recall`` queries under the canonical form;
    ``delete_by_subject`` was the only seam left raw, making the
    storage primitive asymmetric.  An RFC 0013 ``SubjectErasure``
    caller passing ``"Bob"`` (the spelling its audit log emitted at
    write time) would silently match zero rows — the failure mode the
    L-2 docstring on ``store`` names "the future RFC 0013 erasure
    backfill" but only fences off on the *insert* seam.

    Scope: the ``subject`` column traversal canonicalises; the
    ``source_interaction_id`` column does **not** — that column holds
    opaque UUIDs and casefolding a UUID is a category error.
    """

    async def test_mixed_case_delete_hits_canonical_row(
        self,
        fact_store: FactStore,
    ) -> None:
        """Store under canonical ``"bob"``; delete via ``"Bob"`` —
        the row is removed (pre-PR-5d this returned ``0`` and the row
        stayed live)."""
        await fact_store.store(
            subject="bob",
            predicate="prefers",
            object="tea",
            source_interaction_id="ix-1",
            asserted_at=1000.0,
        )
        result = await fact_store.delete_by_subject("Bob")
        assert result["facts_deleted_by_subject"] == 1, (
            "mixed-case delete must canonicalise and hit the "
            "canonical row; pre-PR-5d this returned 0 (silent miss)"
        )
        # Row is genuinely gone — a follow-up recall returns empty.
        assert await fact_store.recall(subject="bob") == []

    async def test_whitespace_delete_hits_canonical_row(
        self,
        fact_store: FactStore,
    ) -> None:
        """Whitespace-bearing input canonicalises the same way the
        write path does, so the delete hits the canonical row."""
        await fact_store.store(
            subject="bob",
            predicate="prefers",
            object="tea",
            source_interaction_id="ix-1",
            asserted_at=1000.0,
        )
        result = await fact_store.delete_by_subject("  bob  ")
        assert result["facts_deleted_by_subject"] == 1

    async def test_delete_via_canonical_form_still_works(
        self,
        fact_store: FactStore,
    ) -> None:
        """Regression guard — adding canonicalisation must not break
        the fast path where the caller already supplies the canonical
        form (the dominant path today, including the existing
        :class:`tests.unit.python.test_fact_store.TestDeleteBySubject`
        coverage)."""
        await fact_store.store(
            subject="bob",
            predicate="prefers",
            object="tea",
            source_interaction_id="ix-1",
            asserted_at=1000.0,
        )
        result = await fact_store.delete_by_subject("bob")
        assert result["facts_deleted_by_subject"] == 1

    async def test_source_interaction_id_traversal_not_canonicalised(
        self,
        fact_store: FactStore,
    ) -> None:
        """The ``source_interaction_id`` column holds opaque UUIDs,
        not subject strings.  Canonicalising it would casefold a
        mixed-case UUID and silently miss the row — the exact
        failure mode the M-1 fix exists to prevent on the *subject*
        column.  Pin that a mixed-case UUID-shaped value still
        round-trips bit-for-bit on the source-interaction traversal.
        """
        # Use a mixed-case UUID-shaped source_interaction_id to prove
        # the column is not canonicalised on delete.
        mixed_case_ix = "Alice-IX-1-Cafebabe"
        await fact_store.store(
            subject="charlie",
            predicate="prefers",
            object="tea",
            source_interaction_id=mixed_case_ix,
            asserted_at=1000.0,
        )
        # A canonicalised query (lowercased) must NOT match — that
        # would be the silent-miss failure mode on the UUID side.
        miss = await fact_store.delete_by_subject(mixed_case_ix.casefold())
        assert miss["facts_deleted_by_source_interaction"] == 0, (
            "source_interaction_id traversal must preserve the raw "
            "value; canonicalising it would casefold UUIDs and miss"
        )
        # The exact mixed-case form still hits.
        hit = await fact_store.delete_by_subject(mixed_case_ix)
        assert hit["facts_deleted_by_source_interaction"] == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
