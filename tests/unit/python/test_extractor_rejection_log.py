"""Tests for the rejected-predicate discovery log (RFC 0026 PR 2 review).

The predicate allowlist in :mod:`agents.memory.fact_predicates` is the
storage-boundary cap on prompt-injection blast radius, but it is also
the bound on what the LLM can record.  A near-miss verb the model
emits (e.g. ``has_kid_named`` vs the allowlisted ``has_child_named``)
is a quality signal that the vocabulary needs an amendment, not just
a security signal.

The extractor records each distinct rejected verb verbatim, once per
process, into the structured-log surface via the
``persatrix.facts.rejected_predicate`` field.  This file pins:

* The verbatim recording (the verb itself, not a sanitised summary,
  reaches the log).
* The per-process dedup (a second rejection of the same verb is not
  re-logged — the discovery surface is the unique vocabulary, not a
  per-tuple repeat).
* The per-verb scope of dedup (two distinct unknown verbs surface as
  two log lines, not one).
* The negative case (a tuple missing the ``predicate`` key fails
  before the allowlist check and must not pollute the discovery
  surface with non-verb failure modes).

Tests reset the in-process dedup set via the test-only
:func:`_reset_rejected_predicates_seen` so prior tests do not mask a
fresh emission.
"""

from __future__ import annotations

import pytest

from agents.memory.facts import FactStore
from agents.persona_runtime import fact_extractor as fx
from agents.persona_runtime.fact_extractor import (
    _reset_rejected_predicates_seen,
    store_extracted_facts,
)


@pytest.fixture
async def fact_store():
    store = FactStore(agent_id="test-agent", db_path=":memory:")
    await store.initialize()
    yield store
    await store.close()


@pytest.mark.asyncio
class TestRejectedPredicateLog:
    async def test_rejected_predicate_logged_verbatim(
        self, fact_store: FactStore, caplog: pytest.LogCaptureFixture,
    ):
        """When the LLM emits a verb the allowlist rejects, the verb
        itself reaches the log so operators can mine recurring
        patterns and grow the vocabulary from observed workload (not
        guesses).  The log line carries the predicate verbatim as a
        structured field — the existing tuple-rejection warning logs
        the whole raw dict (PII-bearing object included) and is too
        noisy / context-sensitive for aggregation."""
        _reset_rejected_predicates_seen()
        caplog.set_level("INFO", logger="agents.persona_runtime.fact_extractor")
        await store_extracted_facts(
            fact_store,
            facts=[
                {
                    "subject": "bob",
                    "predicate": "manifests_unauthorised_powers",
                    "object": "telekinesis",
                },
            ],
            source_interaction_id="ix-1",
            asserted_at=1000.0,
            session_id="legacy",
        )
        rejected_records = [
            rec for rec in caplog.records
            if getattr(rec, "persatrix.facts.rejected_predicate", None)
            == "manifests_unauthorised_powers"
        ]
        assert len(rejected_records) == 1, (
            "exactly one structured-field log line per (process, predicate)"
        )

    async def test_repeated_rejected_predicate_logged_once(
        self, fact_store: FactStore, caplog: pytest.LogCaptureFixture,
    ):
        """Per-process dedup — the second rejection of the same verb
        does not re-log.  Bounds log volume so the discovery surface
        is the unique vocabulary, not a per-tuple repeat."""
        _reset_rejected_predicates_seen()
        caplog.set_level("INFO", logger="agents.persona_runtime.fact_extractor")
        for _ in range(3):
            await store_extracted_facts(
                fact_store,
                facts=[
                    {
                        "subject": "bob",
                        "predicate": "manifests_unauthorised_powers",
                        "object": "telekinesis",
                    },
                ],
                source_interaction_id="ix-1",
                asserted_at=1000.0,
                session_id="legacy",
            )
        rejected_records = [
            rec for rec in caplog.records
            if getattr(rec, "persatrix.facts.rejected_predicate", None)
            == "manifests_unauthorised_powers"
        ]
        assert len(rejected_records) == 1, (
            "dedup must keep the first occurrence and drop the rest"
        )

    async def test_distinct_rejected_predicates_each_logged_once(
        self, fact_store: FactStore, caplog: pytest.LogCaptureFixture,
    ):
        """Two distinct unknown verbs surface as two log lines —
        dedup is per-verb, not blanket suppression."""
        _reset_rejected_predicates_seen()
        caplog.set_level("INFO", logger="agents.persona_runtime.fact_extractor")
        await store_extracted_facts(
            fact_store,
            facts=[
                {"subject": "bob", "predicate": "verb_alpha", "object": "x"},
                {"subject": "bob", "predicate": "verb_beta", "object": "y"},
            ],
            source_interaction_id="ix-1",
            asserted_at=1000.0,
            session_id="legacy",
        )
        rejected = {
            getattr(rec, "persatrix.facts.rejected_predicate", None)
            for rec in caplog.records
        }
        assert "verb_alpha" in rejected
        assert "verb_beta" in rejected

    async def test_missing_predicate_field_does_not_log_rejected_verb(
        self, fact_store: FactStore, caplog: pytest.LogCaptureFixture,
    ):
        """A tuple missing the ``predicate`` key fails before the
        allowlist check; there is no rejected verb to record.  Keeps
        the discovery surface free of non-verb failure modes."""
        _reset_rejected_predicates_seen()
        caplog.set_level("INFO", logger="agents.persona_runtime.fact_extractor")
        await store_extracted_facts(
            fact_store,
            facts=[{"subject": "bob", "object": "x"}],  # no predicate
            source_interaction_id="ix-1",
            asserted_at=1000.0,
            session_id="legacy",
        )
        rejected = [
            rec for rec in caplog.records
            if hasattr(rec, "persatrix.facts.rejected_predicate")
        ]
        assert rejected == []

    async def test_dedup_cap_saturates_silently(
        self, fact_store: FactStore, caplog: pytest.LogCaptureFixture,
    ):
        """PR #340 review coverage gap — the cap on the dedup set
        bounds memory against a pathological LLM emitting unique-per-
        call rejection garbage.  Once the set holds
        ``_REJECTED_PREDICATES_LOG_CAP`` distinct strings, a further
        distinct verb is **silently dropped** — not logged, not added
        to the set.  The discovery surface gets the first N verbs the
        process saw; further unique noise is bounded.

        We exercise the bound directly via the module-private set so
        the test does not have to write 256 fact rows (the per-tuple
        WARNING and the stored-count would dominate the log signal we
        want to assert on).  The module-private import is justified —
        this is a process-scoped invariant the public API does not
        expose, and the test exists to pin the saturation contract
        documented on the ``_REJECTED_PREDICATES_LOG_CAP`` constant.
        """
        _reset_rejected_predicates_seen()
        # Pre-fill the dedup set to the cap with sentinel verbs that
        # never collide with the one we care about.
        for i in range(fx._REJECTED_PREDICATES_LOG_CAP):
            fx._REJECTED_PREDICATES_SEEN.add(f"__sentinel_{i}__")
        assert (
            len(fx._REJECTED_PREDICATES_SEEN)
            == fx._REJECTED_PREDICATES_LOG_CAP
        )
        caplog.set_level("INFO", logger="agents.persona_runtime.fact_extractor")
        await store_extracted_facts(
            fact_store,
            facts=[
                {
                    "subject": "bob",
                    "predicate": "verb_after_cap",
                    "object": "x",
                },
            ],
            source_interaction_id="ix-1",
            asserted_at=1000.0,
            session_id="legacy",
        )
        # The verb is rejected (it is not in the allowlist) but the
        # discovery log does not fire — the per-process dedup set is
        # already at its cap.
        rejected = [
            rec for rec in caplog.records
            if getattr(rec, "persatrix.facts.rejected_predicate", None)
            == "verb_after_cap"
        ]
        assert rejected == [], (
            "verb past the dedup cap must NOT reach the discovery log"
        )
        # And the set must not have grown past the cap.
        assert (
            len(fx._REJECTED_PREDICATES_SEEN)
            == fx._REJECTED_PREDICATES_LOG_CAP
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
