"""RFC 0026 PR 4 — Reinforcement + retraction + self-admission + multi-subject.

Pins the PR 4 deliverables called out in :doc:`docs/rfcs/0026-pr-plan.md`
§PR 4:

* **Reinforcement**: ``last_recalled_at`` advances on every
  :class:`~agents.persona_runtime.memory_budget.MemoryBudget`-admitted
  fact.  Composes with :doc:`RFC 0008 §G
  <../../docs/rfcs/0008-agent-memory-context-optimization>` decay /
  validation via the same scoring seam; the calibration formula lands
  in :doc:`RFC 0008 calibration review
  <../../docs/rfcs/0008-calibration-review>`, this PR ships only the
  write.

* **Retraction**: latest-asserted-wins via the
  :meth:`agents.memory.facts.FactStore.store` supersede-on-insert
  branch (already in PR 1).  Superseded rows are absent from default
  recall, so the admission set surfaces only the live row — the
  dementia-test invariant that a contradicted fact never leaks back
  into the prompt.

* **Self admission** (OQ #10): ``self.*`` facts admit at recall time
  alongside facts about the sender.  Required so MT-MEMORY-005 Leg 5
  (self-consistency) flips green — the previous shape seeded only
  from ``event.sender_id``, leaving introspective rows write-only.

* **Multi-subject section shape**: once two seeds (``self`` + sender)
  produce facts, the section renders one block per subject so a
  ``self.has_preference`` row is not silently labelled under the
  sender's header (the persona-inversion footgun the dementia test
  is meant to fence off — see :doc:`docs/rfcs/0026-pr-plan.md`
  PR 3 review M-2 carry-over).

* **Tier-provenance instrumentation**: every admitted fact appears
  in :meth:`MemoryBudget.admissions_by_tier` so MT-MEMORY-005 leg-
  failure analyses can disambiguate recall miss from reasoning miss
  (the MQ-11 deliverable).
"""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.memory.episodic import EpisodicMemory
from agents.memory.facts import FactStore
from agents.memory.working import WorkingMemory
from agents.persona_runtime.memory_context import _MemoryContextMixin
from agents.persona_types import EventType


pytestmark = pytest.mark.asyncio


# ─── Fixtures / harness ─────────────────────────────────────


@pytest.fixture
async def fact_store() -> AsyncGenerator[FactStore, None]:
    store = FactStore(agent_id="dementia-agent", db_path=":memory:")
    await store.initialize()
    try:
        yield store
    finally:
        await store.close()


@pytest.fixture
async def empty_episodic() -> AsyncGenerator[EpisodicMemory, None]:
    mem = EpisodicMemory(agent_id="dementia-agent", db_path=":memory:")
    await mem.initialize()
    try:
        yield mem
    finally:
        await mem.close()


@dataclass
class _FakeRelSummary:
    other_participant_id: str = "bob"
    other_participant_type: str = "user"
    interaction_count: int = 0
    trust_score: float = 0.5
    notes: str | None = None
    last_interaction_at: float | None = None
    first_interaction_at: float | None = None


class _ConcreteMemoryMixin(_MemoryContextMixin):
    def __init__(self, *, format_query: str = "") -> None:
        from agents.clock import WallClock  # noqa: PLC0415

        super().__init__()
        self._clock = WallClock()
        self._timezone = "UTC"
        self._format_query = format_query

    def _format_event(self, event: Any) -> str:  # type: ignore[override]
        payload = getattr(event, "payload", {}) or {}
        if self._format_query:
            return self._format_query
        return str(payload.get("content", ""))


def _wire_mixin(
    *,
    fact_store: FactStore | None,
    episodic: EpisodicMemory,
    rel: _FakeRelSummary | None = None,
    format_query: str = "",
    facts_enabled: bool = True,
) -> _ConcreteMemoryMixin:
    mixin = _ConcreteMemoryMixin(format_query=format_query)
    mixin.agent_id = "dementia-agent"
    mixin._working_memory = WorkingMemory(max_tokens=8192)
    mixin._episodic_memory = episodic
    mixin._relationship_memory = AsyncMock()
    mixin._relationship_memory.get_relationship_summary.return_value = rel
    mixin._fact_store = fact_store
    mixin._facts_enabled = facts_enabled
    mixin._facts_budget_tokens = 200
    return mixin


def _make_event(
    *,
    sender_id: str | None = "bob",
    content: str = "tell me about yourself",
    event_type: EventType = EventType.CHANNEL_MESSAGE,
) -> Any:
    event = MagicMock()
    event.event_type = event_type
    event.channel_id = None
    event.sender_id = sender_id
    event.thread_id = None
    event.metadata = {}
    event.payload = {"content": content}
    event.timestamp = 0.0
    return event


# ─── 1. Reinforcement: last_recalled_at advances on admit ──


class TestReinforcement:
    async def test_last_recalled_at_set_on_admit(
        self, fact_store: FactStore, empty_episodic: EpisodicMemory,
    ) -> None:
        await fact_store.store(
            subject="bob",
            predicate="has_child_named",
            object="Mira",
            source_interaction_id="i1",
            asserted_at=1000.0,
        )
        rows_before = await fact_store.recall(subject="bob")
        assert rows_before[0].last_recalled_at is None

        mixin = _wire_mixin(
            fact_store=fact_store, episodic=empty_episodic,
            format_query="how are things",
        )
        event = _make_event(sender_id="bob", content="how are things")
        await mixin._inject_memory_context(event)

        rows_after = await fact_store.recall(subject="bob")
        assert rows_after[0].last_recalled_at is not None

    async def test_dropped_fact_does_not_get_reinforced(
        self, fact_store: FactStore, empty_episodic: EpisodicMemory,
    ) -> None:
        """A fact the budget drops (per-tier slice exhausted) must not
        have ``last_recalled_at`` written — reinforcement counts admits,
        not recalls."""
        ts = time.time()
        # Pump enough facts to overflow the 200-token per-tier slice;
        # the tail of the list will be dropped by the allocator.
        fact_ids: list[str] = []
        for i in range(40):
            fid = await fact_store.store(
                subject="bob",
                predicate="self.has_attribute",
                object=f"detail #{i}: " + ("alpha bravo charlie " * 8),
                source_interaction_id=f"i{i}",
                asserted_at=ts + i,
            )
            fact_ids.append(fid)

        mixin = _wire_mixin(
            fact_store=fact_store, episodic=empty_episodic,
            format_query="what's new",
        )
        event = _make_event(sender_id="bob", content="what's new")
        await mixin._inject_memory_context(event)

        # Inspect every stored row.  At least one row must have been
        # dropped (the 40-fact load is larger than the per-tier slice)
        # and at least one must have been admitted; the dropped row's
        # ``last_recalled_at`` stays ``None``.
        all_rows = await fact_store.recall(
            subject="bob", limit=100, include_superseded=True,
        )
        admitted = [r for r in all_rows if r.last_recalled_at is not None]
        dropped = [r for r in all_rows if r.last_recalled_at is None]
        assert admitted, "no fact was reinforced — allocator regressed"
        assert dropped, "no fact was dropped — per-tier slice broken"


# ─── 2. Retraction: latest-asserted-wins ───────────────────


class TestRetraction:
    async def test_superseded_row_absent_from_default_recall(
        self, fact_store: FactStore,
    ) -> None:
        await fact_store.store(
            subject="bob", predicate="prefers", object="coffee",
            source_interaction_id="i1", asserted_at=1000.0,
        )
        await fact_store.store(
            subject="bob", predicate="prefers", object="tea",
            source_interaction_id="i2", asserted_at=2000.0,
        )
        rows = await fact_store.recall(subject="bob")
        assert len(rows) == 1
        assert rows[0].object == "tea"

    async def test_only_live_row_appears_in_admitted_section(
        self, fact_store: FactStore, empty_episodic: EpisodicMemory,
    ) -> None:
        """The dementia-test invariant: contradicted facts never leak
        back into the prompt.  PR 1 ships the storage-layer filter; PR 4
        pins the end-to-end behaviour from the persona-runtime entry
        point.
        """
        await fact_store.store(
            subject="bob", predicate="prefers", object="coffee",
            source_interaction_id="i1", asserted_at=1000.0,
        )
        await fact_store.store(
            subject="bob", predicate="prefers", object="tea",
            source_interaction_id="i2", asserted_at=2000.0,
        )
        mixin = _wire_mixin(
            fact_store=fact_store, episodic=empty_episodic,
            format_query="hi",
        )
        event = _make_event(sender_id="bob", content="hi")
        await mixin._inject_memory_context(event)

        section = mixin._working_memory.get_section("facts_context")
        assert section is not None
        assert "tea" in section.content
        assert "coffee" not in section.content


# ─── 3. Self admission (OQ #10) ────────────────────────────


class TestSelfAdmission:
    """The ``self`` seed flows through ``_subject_seeds`` so introspective
    ``self.*`` rows admit at recall time — required so MT-MEMORY-005 Leg
    5 (self-consistency) is testable.  PR 3 wrote ``self.*`` rows but
    did not seed ``self`` at recall time.
    """

    async def test_self_fact_admits_when_sender_has_no_facts(
        self, fact_store: FactStore, empty_episodic: EpisodicMemory,
    ) -> None:
        await fact_store.store(
            subject="self",
            predicate="self.has_preference",
            object="sci-fi",
            source_interaction_id="i1",
            asserted_at=1000.0,
        )
        mixin = _wire_mixin(
            fact_store=fact_store, episodic=empty_episodic,
            format_query="recommend me a book",
        )
        event = _make_event(
            sender_id="bob", content="recommend me a book",
        )
        await mixin._inject_memory_context(event)

        section = mixin._working_memory.get_section("facts_context")
        assert section is not None, "self facts not admitted"
        assert "sci-fi" in section.content
        assert "self.has_preference" in section.content

    async def test_self_fact_reinforced_on_admit(
        self, fact_store: FactStore, empty_episodic: EpisodicMemory,
    ) -> None:
        """Leg 5's pass condition is the self fact stays stable across
        interactions.  Reinforcement must fire on ``self.*`` rows too,
        not only on counterparty rows — otherwise the RFC 0008 decay
        seam will evict the self claim on a slow scale."""
        await fact_store.store(
            subject="self",
            predicate="self.has_preference",
            object="sci-fi",
            source_interaction_id="i1",
            asserted_at=1000.0,
        )
        mixin = _wire_mixin(
            fact_store=fact_store, episodic=empty_episodic,
            format_query="recommend me a book",
        )
        event = _make_event(
            sender_id="bob", content="recommend me a book",
        )
        await mixin._inject_memory_context(event)

        rows = await fact_store.recall(subject="self")
        assert rows[0].last_recalled_at is not None


# ─── 4. Multi-subject section labels each block ────────────


class TestMultiSubjectSection:
    """Once ``_subject_seeds`` yields more than one seed (``self`` plus
    the canonical sender), the rendered section must split into one
    labelled block per subject.  A single ``"Known facts about bob:"``
    header over a list that also contains ``- self self.has_preference
    sci-fi`` invites the LLM to read the sci-fi preference as a fact
    about *bob* — the dementia-test fence is exactly this kind of
    persona-inversion gap."""

    async def test_both_subjects_labelled_in_their_own_block(
        self, fact_store: FactStore, empty_episodic: EpisodicMemory,
    ) -> None:
        await fact_store.store(
            subject="bob",
            predicate="has_child_named",
            object="Mira",
            source_interaction_id="i1",
            asserted_at=1000.0,
        )
        await fact_store.store(
            subject="self",
            predicate="self.has_preference",
            object="sci-fi",
            source_interaction_id="i2",
            asserted_at=1001.0,
        )

        mixin = _wire_mixin(
            fact_store=fact_store, episodic=empty_episodic,
            format_query="hi",
        )
        event = _make_event(sender_id="bob", content="hi")
        await mixin._inject_memory_context(event)

        section = mixin._working_memory.get_section("facts_context")
        assert section is not None
        content = section.content
        # Each subject gets its own header — neither is rendered under
        # the other's banner.
        assert "Known facts about bob:" in content
        assert "Known facts about self:" in content

        # The ``self`` fact line appears under the ``self`` header,
        # not under the ``bob`` header.
        bob_header_idx = content.index("Known facts about bob:")
        self_header_idx = content.index("Known facts about self:")
        bob_line_idx = content.index("Mira")
        self_line_idx = content.index("sci-fi")
        if bob_header_idx < self_header_idx:
            assert bob_header_idx < bob_line_idx < self_header_idx
            assert self_header_idx < self_line_idx
        else:
            assert self_header_idx < self_line_idx < bob_header_idx
            assert bob_header_idx < bob_line_idx

    async def test_single_subject_still_renders_one_block(
        self, fact_store: FactStore, empty_episodic: EpisodicMemory,
    ) -> None:
        """Regression guard: when only one seed has facts, the section
        still has exactly one header — the multi-subject fan-out must
        not add an empty ``"Known facts about self:"`` block when no
        ``self.*`` rows exist."""
        await fact_store.store(
            subject="bob", predicate="prefers", object="tea",
            source_interaction_id="i1", asserted_at=1000.0,
        )
        mixin = _wire_mixin(
            fact_store=fact_store, episodic=empty_episodic,
            format_query="hi",
        )
        event = _make_event(sender_id="bob", content="hi")
        await mixin._inject_memory_context(event)

        section = mixin._working_memory.get_section("facts_context")
        assert section is not None
        assert section.content.count("Known facts about") == 1
        assert "Known facts about bob:" in section.content
        assert "Known facts about self:" not in section.content


# ─── 5. Tier-provenance instrumentation (MQ-11) ────────────


class TestTierProvenance:
    """Every admitted fact lands on
    :meth:`MemoryBudget.admissions_by_tier` so MT-MEMORY-005 leg-failure
    analyses can grep the per-turn admission set without re-walking the
    storage layer.  The MQ-11 deliverable.
    """

    async def test_admitted_fact_ids_are_recorded_on_budget(
        self, fact_store: FactStore, empty_episodic: EpisodicMemory,
    ) -> None:
        # The mixin allocates its own budget per-event; we need to
        # observe it after ``_inject_memory_context`` runs.  Patch
        # the constructor to capture the budget instance.
        from agents.persona_runtime import (  # noqa: PLC0415
            memory_context as mc_mod,
        )

        captured: list[Any] = []
        original_cls = mc_mod.MemoryBudget

        class _CapturingBudget(original_cls):  # type: ignore[misc, valid-type]
            def __init__(self, total_tokens: int) -> None:
                super().__init__(total_tokens=total_tokens)
                captured.append(self)

        mc_mod.MemoryBudget = _CapturingBudget  # type: ignore[misc]
        try:
            fid_1 = await fact_store.store(
                subject="bob",
                predicate="has_child_named",
                object="Mira",
                source_interaction_id="i1",
                asserted_at=1000.0,
            )
            fid_2 = await fact_store.store(
                subject="bob",
                predicate="prefers",
                object="tea",
                source_interaction_id="i2",
                asserted_at=1001.0,
            )

            mixin = _wire_mixin(
                fact_store=fact_store, episodic=empty_episodic,
                format_query="hi",
            )
            event = _make_event(sender_id="bob", content="hi")
            await mixin._inject_memory_context(event)
        finally:
            mc_mod.MemoryBudget = original_cls  # type: ignore[misc]

        assert captured, "MemoryBudget was not constructed"
        budget = captured[0]
        admitted_facts = budget.admissions_by_tier("facts")
        # Both fact_ids must appear in admission order.
        assert set(admitted_facts) == {fid_1, fid_2}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
