"""Tests for PR #340 review follow-ups on the RFC 0026 PR 2 extractor.

Split out of :mod:`tests.unit.python.test_extractor` so both files
stay under the 500-line review-friendly cap enforced by
``scripts/checks/file_size.py``.  This module groups the coverage
adds and the S1 sender-id-canonical-return pin that landed after the
deep review of PR #340 — keeping them as a separately-named file
makes the intent visible in the diff history without forcing the
parent test module past the cap.

Pinned contracts
----------------
* **S1 — sender-id substitution returns canonical, not raw.** The
  substitution branch in
  :func:`agents.persona_runtime.fact_extractor._canonicalize_subject`
  yields the canonical form of ``sender_id`` so a mixed-case
  caller-supplied identifier does not split rows across casings.
* **Coverage — boolean certainty rejection.**
  :func:`agents.persona_runtime.fact_extractor._coerce_certainty`
  rejects ``certainty=True`` / ``False`` even though ``bool`` is an
  ``int`` subclass — a boolean here is usually LLM-side type
  confusion that should surface as a counter bump, not coerce to
  ``1.0`` silently.
* **Coverage — non-string summary rejection.**
  :func:`agents.persona_runtime.fact_extractor.split_combined_response`
  rejects ``{"summary": 42}`` / ``{"summary": null}`` the same way it
  rejects a missing key, so a downstream caller does not commit a
  non-string column value.
* **S3 — whitespace-only sender_id is inert at the boundary.**
  :func:`agents.persona_runtime.fact_extractor.store_extracted_facts`
  normalises an empty / whitespace-only ``sender_id`` to ``None`` at
  the function boundary so a direct caller (test fixture or future
  operator-seeded write path) cannot reach
  :func:`canonicalize_subject` with whitespace input.  Without this
  pin, the ``ValueError`` raised by ``canonicalize_subject`` would
  fire *before* the per-tuple try-block, escape under
  :func:`dispatch_facts_from_response`'s broad ``except``, and drop
  the entire batch with no ``agent.facts.extraction_failed`` counter
  bump — silent data loss indistinguishable from "no facts
  extracted."  The close-path :func:`_interaction_sender` already
  filters whitespace senders to ``None``, so this is defence-in-depth
  for non-close-path callers.
"""

from __future__ import annotations

import json

import pytest
from _otel_test_helpers import counter_total

from agents.memory.facts import FactStore
from agents.persona_runtime.fact_extractor import (
    FactsParseError,
    split_combined_response,
    store_extracted_facts,
)

# ─── Fixtures ───────────────────────────────────────────────


@pytest.fixture
async def fact_store():
    store = FactStore(agent_id="test-agent", db_path=":memory:")
    await store.initialize()
    yield store
    await store.close()


def _build_meter():
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    from agents.observability import metrics as metrics_mod

    reader = InMemoryMetricReader()
    metrics_mod.init_metrics(reader=reader)
    return reader, metrics_mod


# ─── S1: sender-id substitution canonicalises return ────────


@pytest.mark.asyncio
class TestSenderIdSubstitutionReturnsCanonical:
    """PR #340 review S1 — the sender-id substitution branch in
    :func:`_canonicalize_subject` returns the **canonical** form of
    ``sender_id``, not the raw mixed-case caller-supplied string.

    Why this matters: the relationship row keys on the same subject
    column.  A mixed-case ``sender_id`` substituted verbatim would
    split rows across casings — one canonical ``"bob_user_123"`` row
    from any other write path, one raw ``"Bob_user_123"`` row from
    this substitution branch.  Both should converge to the canonical
    key so the dementia-test round-trip joins on a single
    ``facts.subject`` column.
    """

    async def test_sender_id_substitution_canonicalises_return(
        self, fact_store: FactStore,
    ):
        """The substitution branch fires when the canonicalised
        LLM-emitted subject matches the canonicalised ``sender_id``.
        We pick a mixed-case ``sender_id`` (``"Bob_user_123"``) and an
        LLM-emitted subject (``"Bob_User_123"``) that fold to the same
        canonical form; the resulting row's subject column must be the
        canonical form (post-S1-fix), not the raw ``sender_id``.

        PR 5c L-2 update: :meth:`FactStore.recall` now canonicalises
        its query subject too, so both the canonical and raw mixed-
        case forms hit the same row — pinning the dementia-test
        round-trip invariant ("a single canonical row per
        counterparty regardless of caller spelling") at both the
        write and read seams.
        """
        await store_extracted_facts(
            fact_store,
            facts=[
                {
                    # Mixed-case spelling that canonicalises to the
                    # same form as the sender_id below — triggers the
                    # substitution branch.
                    "subject": "Bob_User_123",
                    "predicate": "has_name",
                    "object": "Bob",
                },
            ],
            source_interaction_id="ix-1",
            asserted_at=1000.0,
            session_id="legacy",
            sender_id="Bob_user_123",
        )
        # The row lives under the canonical form regardless of the
        # caller-supplied spelling.  PR 5c's L-2 fix means recall
        # canonicalises its query too, so a mixed-case query hits
        # the same row — the single-row-per-counterparty invariant
        # is preserved at both the store and the recall seams.
        canonical_hits = await fact_store.recall(subject="bob_user_123")
        assert len(canonical_hits) == 1, (
            "row must land under canonical sender_id"
        )
        assert canonical_hits[0].subject == "bob_user_123", (
            "row must be stored under the canonical sender_id"
        )
        raw_hits = await fact_store.recall(subject="Bob_user_123")
        assert len(raw_hits) == 1 and raw_hits[0].subject == "bob_user_123", (
            "recall query canonicalises so the mixed-case form hits "
            "the same canonical row (PR 5c L-2)"
        )


# ─── Coverage: _coerce_certainty bool rejection ─────────────


@pytest.mark.asyncio
class TestCertaintyBooleanRejected:
    """PR #340 review coverage gap — ``certainty=True`` is rejected
    explicitly because ``bool`` is a subclass of ``int`` and would
    otherwise coerce to ``1.0`` without surfacing the LLM's type
    confusion.  The branch is in
    :func:`agents.persona_runtime.fact_extractor._coerce_certainty`
    and the existing rejection-counter test surface
    (``agent.facts.extraction_failed``) is the observable signal."""

    async def test_certainty_true_rejected_counter_bumps(
        self, fact_store: FactStore,
    ):
        reader, metrics_mod = _build_meter()
        try:
            n = await store_extracted_facts(
                fact_store,
                facts=[
                    {
                        "subject": "bob",
                        "predicate": "has_name",
                        "object": "Bob",
                        "certainty": True,  # bool — rejected
                    },
                    {
                        "subject": "bob",
                        "predicate": "lives_in",
                        "object": "Berlin",
                        "certainty": 0.8,  # valid float
                    },
                ],
                source_interaction_id="ix-1",
                asserted_at=1000.0,
                session_id="legacy",
            )
            assert n == 1, "bool-certainty tuple skipped; good tuple stored"
            assert counter_total(
                reader, "agent.facts.extraction_failed",
            ) == 1
        finally:
            await metrics_mod.shutdown()


# ─── Coverage: split_combined_response summary-shape ────────


class TestSplitCombinedResponseSummaryShape:
    """PR #340 review coverage gap — pin that a non-string ``summary``
    field is rejected the same way as a missing one.

    The summary field is load-bearing; a JSON ``42`` or ``null`` slips
    through the existing ``"summary" not in envelope`` check and would
    otherwise reach :class:`agents.memory.episodic.EpisodicMemory` as a
    non-string column value.  The explicit type check at parse time
    surfaces the LLM drift as a :class:`FactsParseError` so the caller
    commits no summary at all rather than a broken one.
    """

    def test_summary_integer_raises(self) -> None:
        with pytest.raises(FactsParseError):
            split_combined_response(json.dumps({"summary": 42}))

    def test_summary_null_raises(self) -> None:
        with pytest.raises(FactsParseError):
            split_combined_response(json.dumps({"summary": None}))


# ─── S3: whitespace sender_id normalised at boundary ────────


@pytest.mark.asyncio
class TestWhitespaceSenderIdNormalisedAtBoundary:
    """PR #340 deep-review S3 — a whitespace-only ``sender_id`` must
    not abort the batch.

    Why this matters: the canonical_sender precompute in
    :func:`store_extracted_facts` runs *before* the per-tuple
    ``try/except`` block.  A whitespace-only ``sender_id`` would
    satisfy the truthiness check (``"   "`` is truthy in Python) and
    reach :func:`canonicalize_subject`, which raises
    ``ValueError("subject must not be empty")``.  The ``ValueError``
    escapes the helper, is caught by
    :func:`dispatch_facts_from_response`'s broad ``except Exception``,
    and the entire batch is dropped with no per-tuple
    ``agent.facts.extraction_failed`` increment.  Operators see one
    WARNING that looks identical to an infra failure — no signal that
    the LLM emitted N tuples that all got silently lost.

    Reachability: the production close-path
    (:func:`_interaction_sender`) already filters whitespace senders
    to ``None``, so this is a defence-in-depth pin for direct callers
    — test fixtures today, and the future RFC 0026 OQ #9
    operator-seeded write path tomorrow.  Tests rather than docstring
    promises because the contract belongs to the function boundary
    not the close-path entry point.
    """

    async def test_whitespace_sender_id_stores_facts_normally(
        self, fact_store: FactStore,
    ):
        """Pre-fix: ``canonicalize_subject("   ")`` raised before the
        per-tuple loop, dropping the whole batch.  Post-fix: the
        whitespace ``sender_id`` is treated as ``None`` and the fact
        processes normally.
        """
        n = await store_extracted_facts(
            fact_store,
            facts=[
                {
                    "subject": "alice",
                    "predicate": "has_name",
                    "object": "Alice",
                },
            ],
            source_interaction_id="ix-1",
            asserted_at=1000.0,
            session_id="legacy",
            sender_id="   ",
        )
        assert n == 1, (
            "fact must commit; whitespace-only sender_id is inert "
            "(treated as None)"
        )

    async def test_whitespace_sender_id_skips_substitution(
        self, fact_store: FactStore,
    ):
        """With ``sender_id`` whitespace-only, the sender-id
        substitution branch in :func:`_canonicalize_subject` must
        *not* fire — the fact lands under the canonical form of the
        LLM-emitted subject, not coerced to any "sender" form.
        """
        await store_extracted_facts(
            fact_store,
            facts=[
                {
                    "subject": "Alice",
                    "predicate": "has_name",
                    "object": "Alice",
                },
            ],
            source_interaction_id="ix-1",
            asserted_at=1000.0,
            session_id="legacy",
            sender_id="   ",
        )
        hits = await fact_store.recall(subject="alice")
        assert len(hits) == 1, (
            "fact must land under canonical subject form; substitution "
            "branch must not fire on whitespace sender_id"
        )

    async def test_empty_string_sender_id_stores_facts_normally(
        self, fact_store: FactStore,
    ):
        """An empty string ``sender_id`` is already handled by the
        existing ``if sender_id`` truthiness check pre-fix — pin the
        regression so the S3 boundary normalisation does not
        accidentally invert it.
        """
        n = await store_extracted_facts(
            fact_store,
            facts=[
                {
                    "subject": "alice",
                    "predicate": "has_name",
                    "object": "Alice",
                },
            ],
            source_interaction_id="ix-1",
            asserted_at=1000.0,
            session_id="legacy",
            sender_id="",
        )
        assert n == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
