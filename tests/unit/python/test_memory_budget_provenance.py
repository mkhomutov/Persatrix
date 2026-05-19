"""Unit tests for :meth:`agents.persona_runtime.memory_budget.MemoryBudget`
per-turn tier-provenance instrumentation (RFC 0026 PR 4 — MQ-11).

PR 4 routes every successful :meth:`MemoryBudget.try_add` admission
through a tier-aware companion call,
:meth:`MemoryBudget.record_admission`, so the allocator owns a per-tier
list of admitted ``item_id`` strings per turn (PR #342 third-pass
review L-1 — ``tokens_admitted`` is consumed by the structured-log
emission only, it does not land on the registry).  Two consumers ride
on this list:

* :doc:`MT-MEMORY-005 dementia test
  <../../../docs/manual-tests/MT-MEMORY-005-dementia-test>`
  leg-failure analysis — operators flip
  ``PERSATRIX_MEMORY_PROVENANCE=1`` to log per-turn provenance and
  disambiguate recall miss (item absent from the admitted slice) from
  reasoning miss (LLM had the row and ignored it).
* :meth:`agents.memory.facts.FactStore.mark_recalled` — the facts
  tier reads the admitted ``fact_id`` list off the budget to write
  ``last_recalled_at`` after the section is built.

Contracts pinned here are pure data-shape contracts — the logging
side-effect (debug-mode env-gated structured-log emission) is covered
by the integration test that asserts on log records under the env
gate; this unit suite covers the in-memory registry alone so the
test surface for the facts-tier reinforcement path stays narrow.
"""

from __future__ import annotations

import logging

import pytest

from agents.persona_runtime.memory_budget import KNOWN_TIERS, MemoryBudget

# ─── Registry shape ────────────────────────────────────────────


class TestRecordAdmissionRegistry:
    def test_empty_budget_has_no_admissions(self) -> None:
        budget = MemoryBudget(total_tokens=1500)
        assert budget.admissions_by_tier("facts") == []
        assert budget.admissions_by_tier("episodic") == []

    def test_record_admission_appends_to_per_tier_list(self) -> None:
        budget = MemoryBudget(total_tokens=1500)
        budget.record_admission(
            tier="facts", item_id="f1", tokens_admitted=10,
        )
        budget.record_admission(
            tier="facts", item_id="f2", tokens_admitted=8,
        )
        assert budget.admissions_by_tier("facts") == ["f1", "f2"]

    def test_admissions_are_partitioned_by_tier(self) -> None:
        budget = MemoryBudget(total_tokens=1500)
        budget.record_admission(
            tier="facts", item_id="f1", tokens_admitted=10,
        )
        budget.record_admission(
            tier="episodic", item_id="e1", tokens_admitted=20,
        )
        budget.record_admission(
            tier="notes", item_id="n1", tokens_admitted=15,
        )
        assert budget.admissions_by_tier("facts") == ["f1"]
        assert budget.admissions_by_tier("episodic") == ["e1"]
        assert budget.admissions_by_tier("notes") == ["n1"]
        assert budget.admissions_by_tier("relationship") == []

    def test_preserves_insertion_order(self) -> None:
        """Reinforcement writes ``last_recalled_at`` in admit-order so the
        first row pulled by recall matches the first row reinforced.
        Caller-order preservation is load-bearing for that assertion.
        """
        budget = MemoryBudget(total_tokens=1500)
        for fid in ["f3", "f1", "f2"]:
            budget.record_admission(
                tier="facts", item_id=fid, tokens_admitted=10,
            )
        assert budget.admissions_by_tier("facts") == ["f3", "f1", "f2"]


# ─── Tier-name allowlist (PR #342 review N-4) ────────────────


class TestKnownTierAllowlist:
    """The ``tier`` kwarg to :meth:`record_admission` is checked against a
    frozen allowlist so a typo at a future call site fails loudly rather
    than silently populating an unread bucket.  Two failure modes the
    allowlist closes:

    1. ``record_admission(tier="fact", …)`` (singular typo) silently
       populates ``_admissions["fact"]`` while the facts-tier
       reinforcement read at
       :meth:`agents.memory.facts.FactStore.mark_recalled` looks up
       ``admissions_by_tier("facts")`` (plural) — returns ``[]`` —
       and the reinforcement write is skipped without surfacing
       anywhere.  The MT-MEMORY-005 leg-failure attribution log would
       then show "0 facts admitted" even when the section was
       successfully built and rendered.
    2. The shared :data:`KNOWN_TIERS` constant gives tests a single
       source of truth for the canonical names instead of bare string
       literals scattered across the codebase, matching the pattern
       :data:`agents.memory.fact_predicates.PREDICATE_ALLOWLIST`
       establishes for the storage layer.
    """

    def test_known_tiers_constant_covers_all_canonical_names(self) -> None:
        """:data:`KNOWN_TIERS` is the single source of truth.  This test
        pins the membership so adding a new tier in production code
        without updating the allowlist surfaces as a deliberate
        review touch-point rather than slipping through silently.
        """
        # All five tier names appearing in the canonical RFC 0027 §F
        # priority order, plus the relationship tier that does not
        # currently call ``record_admission`` but is part of the same
        # vocab so future wiring lands on a known name.
        assert KNOWN_TIERS == frozenset({
            "facts",
            "episodic",
            "notes",
            "relationship",
            "channel_history",
        })

    @pytest.mark.parametrize("tier", sorted({
        "facts", "episodic", "notes", "relationship", "channel_history",
    }))
    def test_known_tier_admits_silently(self, tier: str) -> None:
        budget = MemoryBudget(total_tokens=1500)
        budget.record_admission(
            tier=tier, item_id="item-1", tokens_admitted=10,
        )
        assert budget.admissions_by_tier(tier) == ["item-1"]

    @pytest.mark.parametrize("typo", [
        "fact",          # singular drift from "facts"
        "Facts",         # case drift
        "episodic_recall",  # section name vs tier name confusion
        "rel",           # abbreviation drift
        "",              # empty string
    ])
    def test_unknown_tier_raises(self, typo: str) -> None:
        budget = MemoryBudget(total_tokens=1500)
        with pytest.raises(ValueError, match="not a known tier"):
            budget.record_admission(
                tier=typo, item_id="item-1", tokens_admitted=10,
            )

    def test_admissions_by_tier_does_not_validate_reader_side(self) -> None:
        """The reader side is intentionally permissive — a typo at a
        *read* site returns ``[]`` (the empty-tier default), not a
        raise.  Only :meth:`record_admission` validates, because the
        write side is where the bug lives: a reader that fishes for
        ``admissions_by_tier("fact")`` and gets ``[]`` is harmless;
        a writer that puts items into ``"fact"`` orphans them.
        """
        budget = MemoryBudget(total_tokens=1500)
        # Must not raise — readers see the empty-default contract.
        assert budget.admissions_by_tier("not-a-tier") == []


# ─── Env-gated structured-log emission ───────────────────────


class TestProvenanceLogEmission:
    """The PERSATRIX_MEMORY_PROVENANCE env gate flips the structured-log
    emission on; production deploys leave it off so the persona's hot
    path does not pay for a log record per admitted item.

    The shape of the emitted record is pinned here so MT-MEMORY-005's
    leg-failure analysis can grep ``persatrix.memory.tier_admitted``
    against the per-turn slice without a translation layer.
    """

    def test_emission_off_by_default(
        self,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("PERSATRIX_MEMORY_PROVENANCE", raising=False)
        budget = MemoryBudget(total_tokens=1500)
        with caplog.at_level(logging.DEBUG, logger="agents.persona_runtime"):
            budget.record_admission(
                tier="facts", item_id="f1", tokens_admitted=10,
            )
        provenance_records = [
            r for r in caplog.records
            if "tier_admitted" in r.getMessage()
        ]
        assert provenance_records == []

    def test_emission_on_when_env_gate_set(
        self,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("PERSATRIX_MEMORY_PROVENANCE", "1")
        budget = MemoryBudget(total_tokens=1500)
        with caplog.at_level(logging.INFO, logger="agents.persona_runtime"):
            budget.record_admission(
                tier="facts", item_id="f1", tokens_admitted=10,
            )
        provenance_records = [
            r for r in caplog.records
            if "tier_admitted" in r.getMessage()
        ]
        assert len(provenance_records) == 1
        rec = provenance_records[0]
        # Structured fields land on the LogRecord as attributes.
        assert getattr(rec, "tier", None) == "facts"
        assert getattr(rec, "item_id", None) == "f1"
        assert getattr(rec, "tokens_admitted", None) == 10

    def test_event_name_is_promoted_to_structured_field(
        self,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The event name is exposed as a structured ``event`` attribute
        on the LogRecord (PR #342 review N-7).

        Operators ingesting these records into structured log pipelines
        (Loki, ELK) can then grep on the ``event`` field instead of
        matching the human-readable message text, which is brittle to
        future message-format changes.  The message field stays
        populated so terminal-tailing the log still works for ad-hoc
        debugging — promoting the event into ``extra`` is additive, not
        a swap.
        """
        monkeypatch.setenv("PERSATRIX_MEMORY_PROVENANCE", "1")
        budget = MemoryBudget(total_tokens=1500)
        with caplog.at_level(logging.INFO, logger="agents.persona_runtime"):
            budget.record_admission(
                tier="facts", item_id="f1", tokens_admitted=10,
            )
        provenance_records = [
            r for r in caplog.records
            if "tier_admitted" in r.getMessage()
        ]
        assert len(provenance_records) == 1
        rec = provenance_records[0]
        assert getattr(rec, "event", None) == "persatrix.memory.tier_admitted"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
