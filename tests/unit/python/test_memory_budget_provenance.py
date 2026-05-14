"""Unit tests for :meth:`agents.persona_runtime.memory_budget.MemoryBudget`
per-turn tier-provenance instrumentation (RFC 0026 PR 4 — MQ-11).

PR 4 routes every successful :meth:`MemoryBudget.try_add` admission
through a tier-aware companion call,
:meth:`MemoryBudget.record_admission`, so the allocator owns a single
list of ``(tier, item_id, tokens_admitted)`` records per turn.  Two
consumers ride on this list:

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

from agents.persona_runtime.memory_budget import MemoryBudget


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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
