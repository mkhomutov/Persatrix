"""RFC 0026 — FactStore.recall wired into the MemoryBudget allocator.

Pins the facts tier's recall + admission contracts: the tier slots
between relationship and notes in the canonical cross-RFC priority
order, recalls per derived subject, and routes admitted rows through
:class:`agents.persona_runtime.memory_budget.MemoryBudget` with a
per-tier floor.  The dementia-test core — a fact stored at interaction
N injected at N+1 *without* the subject string appearing in the query
— is the load-bearing leg of MT-MEMORY-005 and pinned here.

Contracts asserted:

* Subject-indexed recall round-trip; dementia-test core injection.
* Tier ordering — both the ``add_section`` call sequence and the
  ``build_context`` priority-sorted render order (PR 5d).
* ``memory.facts.enabled: false`` skips the tier; no counter tick.
* RFC 0017's 1500-token allocator cap holds with the tier wired in.
* Per-tier header tokens are charged against the budget.
"""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from _otel_test_helpers import build_meter, counter_total

from agents.memory.episodic import EpisodicMemory
from agents.memory.facts import FactStore
from agents.memory.working import WorkingMemory
from agents.persona_runtime.memory_budget import MEMORY_BUDGET_TOKENS
from agents.persona_runtime.memory_context import _MemoryContextMixin
from agents.persona_types import EventType

pytestmark = pytest.mark.asyncio


# ─── Fixtures ─────────────────────────────────────────────────


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
    """Empty episodic store — facts-tier tests must not depend on episodes."""
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
    # Class-level annotations on ``_MemoryContextMixin`` (see
    # ``memory_context.py``: ``_fact_store``, ``_facts_enabled``,
    # ``_facts_budget_tokens``) make these attributes type-checker
    # visible.  PR #341 review N-1: the earlier shape of this fixture
    # carried ``# type: ignore[attr-defined]`` for each line as a
    # hold-over from before the class-level defaults landed — dropped
    # here so the next reader does not assume the attribute surface is
    # private.
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


# ─── 1. Subject-indexed recall round-trip ───────────────────


class TestFactStoreRecallRoundTrip:
    async def test_store_then_recall_returns_fact(
        self, fact_store: FactStore,
    ) -> None:
        await fact_store.store(
            subject="bob",
            predicate="has_child_named",
            object="Mira",
            source_interaction_id="i1",
            asserted_at=time.time(),
        )
        rows = await fact_store.recall(subject="bob")
        assert len(rows) == 1
        assert rows[0].predicate == "has_child_named"
        assert rows[0].object == "Mira"


# ─── 2. Dementia-test core (the load-bearing leg) ──────────


class TestDementiaCore:
    """A fact stored at turn N is admitted to working memory at turn N+1
    even when the natural-language query at N+1 does **not** mention the
    subject string."""

    async def test_fact_injected_without_subject_in_query(
        self, fact_store: FactStore, empty_episodic: EpisodicMemory,
    ) -> None:
        await fact_store.store(
            subject="bob",
            predicate="has_child_named",
            object="Mira",
            source_interaction_id="i1",
            asserted_at=time.time(),
        )

        # The follow-up event's content does NOT contain "bob" — the
        # whole point of the dementia test.  The facts tier keys on
        # the canonical sender, not on text overlap.
        mixin = _wire_mixin(
            fact_store=fact_store,
            episodic=empty_episodic,
            format_query="how are things going today",
        )
        event = _make_event(
            sender_id="bob", content="how are things going today",
        )
        await mixin._inject_memory_context(event)

        section = mixin._working_memory.get_section("facts_context")
        assert section is not None, (
            "facts_context section missing — dementia-test fact lost"
        )
        assert "Mira" in section.content
        assert "has_child_named" in section.content


# ─── 3. Tier ordering: facts before notes ──────────────────


async def _seed_three_tier_mixin(
    fact_store: FactStore, episodic: EpisodicMemory,
) -> _ConcreteMemoryMixin:
    """Seed one fact + one note + a relationship summary and wire a
    mixin so the relationship / facts / notes tiers all admit — shared
    setup for the two tier-ordering pins below.
    """
    await fact_store.store(
        subject="bob", predicate="prefers", object="tea",
        source_interaction_id="i1", asserted_at=time.time(),
    )
    await episodic.store_note(
        topic="rollout", content="bob asked about rollout planning",
    )
    rel = _FakeRelSummary(
        other_participant_id="bob", other_participant_type="user",
        interaction_count=3, trust_score=0.6,
        notes="bob is detail-focused",
        last_interaction_at=time.time() - 60,
        first_interaction_at=time.time() - 3600,
    )
    return _wire_mixin(
        fact_store=fact_store, episodic=episodic, rel=rel,
        format_query="rollout",
    )


class TestTierOrdering:
    async def test_facts_admitted_before_notes_section_added(
        self, fact_store: FactStore, empty_episodic: EpisodicMemory,
    ) -> None:
        """Insertion-order pin: ``add_section`` is called for the facts
        section between ``relationship_context`` and ``recent_notes``.
        The rendered prompt order is pinned separately below.
        """
        mixin = await _seed_three_tier_mixin(fact_store, empty_episodic)

        order: list[str] = []
        real_add = mixin._working_memory.add_section

        def _record(section: Any) -> None:
            order.append(section.name)
            real_add(section)

        mixin._working_memory.add_section = _record  # type: ignore[assignment]

        await mixin._inject_memory_context(
            _make_event(sender_id="bob", content="rollout"),
        )

        rel_idx = order.index("relationship_context")
        facts_idx = order.index("facts_context")
        notes_idx = order.index("recent_notes")
        assert rel_idx < facts_idx < notes_idx, f"tier order drifted: {order}"

    async def test_facts_rendered_between_relationship_and_notes(
        self, fact_store: FactStore, empty_episodic: EpisodicMemory,
    ) -> None:
        """Render-order pin (PR 5d).  The insertion-order test above
        pins the ``add_section`` call sequence; the prompt the LLM sees
        is ``build_context``'s priority-sorted output.  ``facts``
        (priority 7) ties with ``channel_history`` / ``episodic``, so
        the insertion pin matches the prompt only by stable-sort
        coincidence — a ``FACTS_SECTION_PRIORITY`` nudge would leave it
        green while flipping the prompt.  Pins the rendered boundary:
        relationship (8) → facts (7) → notes (6).
        """
        mixin = await _seed_three_tier_mixin(fact_store, empty_episodic)
        await mixin._inject_memory_context(
            _make_event(sender_id="bob", content="rollout"),
        )

        rendered = [
            entry["role"]
            for entry in mixin._working_memory.build_context()
        ]
        rel_idx = rendered.index("relationship_context")
        facts_idx = rendered.index("facts_context")
        notes_idx = rendered.index("recent_notes")
        assert rel_idx < facts_idx < notes_idx, (
            f"rendered tier order drifted: {rendered}"
        )


# ─── 4. Config disable skips the tier ──────────────────────


class TestConfigDisable:
    async def test_disabled_facts_tier_skips_recall_and_counter(
        self, fact_store: FactStore, empty_episodic: EpisodicMemory,
    ) -> None:
        await fact_store.store(
            subject="bob", predicate="prefers", object="tea",
            source_interaction_id="i1", asserted_at=time.time(),
        )

        reader, metrics_mod = build_meter()
        try:
            mixin = _wire_mixin(
                fact_store=fact_store, episodic=empty_episodic,
                facts_enabled=False, format_query="hi",
            )
            event = _make_event(sender_id="bob", content="hi")
            await mixin._inject_memory_context(event)

            # No facts section was added.
            section = mixin._working_memory.get_section("facts_context")
            assert section is None
            # No injection counter increment.
            assert counter_total(reader, "agent.facts.injected") == 0
        finally:
            await metrics_mod.shutdown()


# ─── 5. Token-bound invariant ──────────────────────────────


class TestTokenBoundInvariant:
    """RFC 0017's 1500-token cap must still hold with the facts tier."""

    async def test_admitted_tokens_under_global_cap(
        self, fact_store: FactStore, empty_episodic: EpisodicMemory,
    ) -> None:
        # Pump many facts to try to overflow the budget.
        ts = time.time()
        for i in range(30):
            await fact_store.store(
                subject="bob",
                predicate="self.has_attribute",
                # Each object is verbose to push tokens.
                object=f"detail #{i}: " + ("alpha bravo charlie " * 10),
                source_interaction_id=f"i{i}",
                asserted_at=ts + i,
            )

        mixin = _wire_mixin(
            fact_store=fact_store, episodic=empty_episodic,
            format_query="what's new",
        )
        event = _make_event(sender_id="bob", content="what's new")
        result = await mixin._inject_memory_context(event)

        assert result.memory_admitted_tokens <= MEMORY_BUDGET_TOKENS, (
            f"facts tier broke the 1500-token cap: "
            f"{result.memory_admitted_tokens}"
        )


# ─── 6. Injection counter increments per admitted fact ─────


class TestInjectionCounter:
    async def test_counter_increments_per_admitted_fact(
        self, fact_store: FactStore, empty_episodic: EpisodicMemory,
    ) -> None:
        ts = time.time()
        await fact_store.store(
            subject="bob", predicate="has_child_named", object="Mira",
            source_interaction_id="i1", asserted_at=ts,
        )
        await fact_store.store(
            subject="bob", predicate="prefers", object="tea",
            source_interaction_id="i2", asserted_at=ts + 1,
        )

        reader, metrics_mod = build_meter()
        try:
            mixin = _wire_mixin(
                fact_store=fact_store, episodic=empty_episodic,
                format_query="hi",
            )
            event = _make_event(sender_id="bob", content="hi")
            await mixin._inject_memory_context(event)

            total = counter_total(reader, "agent.facts.injected")
            assert total == 2, (
                f"expected 2 facts.injected increments; saw {total}"
            )
        finally:
            await metrics_mod.shutdown()


# ─── 7. Header tokens charged against the budget ───────────


class TestHeaderChargedAgainstBudget:
    """The ``"Known facts about <subject>:\\n"`` header must consume tokens
    against the budget, so ``memory_admitted_tokens`` does not under-
    report (PR 3 plan: RFC 0017 PR 2 finding #2 regression guard).
    """

    async def test_header_counted_in_admitted_tokens(
        self, fact_store: FactStore, empty_episodic: EpisodicMemory,
    ) -> None:
        await fact_store.store(
            subject="bob", predicate="has_child_named", object="Mira",
            source_interaction_id="i1", asserted_at=time.time(),
        )

        mixin = _wire_mixin(
            fact_store=fact_store, episodic=empty_episodic,
            format_query="hi",
        )
        event = _make_event(sender_id="bob", content="hi")
        result = await mixin._inject_memory_context(event)

        section = mixin._working_memory.get_section("facts_context")
        assert section is not None
        # The admitted tokens reported by the budget should be >= the
        # token count of the section content (the section content
        # includes the header, so the budget must have charged it).
        assert result.memory_admitted_tokens >= section.token_count


# ─── 8. Header text names the subject (review M-2) ─────────


class TestHeaderSubjectTemplated:
    """The section header must name the *subject* of the facts, not
    address the LLM persona as "you" (PR #341 deep-review finding M-2).

    The facts admitted by PR 3 are about the counterparty (the canonical
    ``event.sender_id``), not about the persona itself.  Rendering
    ``"Known facts about you:"`` would invite the LLM to interpret a
    row like ``"bob has_child_named Mira"`` as a fact about *itself*
    (Mira would read as the persona's child) — the persona inversion
    bug the dementia test is supposed to fence off.

    The header is constructed from ``facts[0].subject`` (the canonical
    form already used at the storage layer).  Phase 1 invariant: every
    admitted fact shares one subject because :func:`_subject_seeds`
    yields a single seed (the canonical sender); PR 4 multi-subject
    will refactor the section shape — tracked in
    :doc:`docs/rfcs/0026-pr-plan.md` PR 4 scope.
    """

    async def test_header_names_canonical_subject(
        self, fact_store: FactStore, empty_episodic: EpisodicMemory,
    ) -> None:
        await fact_store.store(
            subject="bob", predicate="has_child_named", object="Mira",
            source_interaction_id="i1", asserted_at=time.time(),
        )

        mixin = _wire_mixin(
            fact_store=fact_store, episodic=empty_episodic,
            format_query="how are things",
        )
        event = _make_event(sender_id="bob", content="how are things")
        await mixin._inject_memory_context(event)

        section = mixin._working_memory.get_section("facts_context")
        assert section is not None
        assert section.content.startswith("Known facts about bob:\n"), (
            f"unexpected header line: {section.content.splitlines()[0]!r}"
        )
        assert "Known facts about you:" not in section.content

    async def test_header_uses_casefolded_subject_for_mixed_case_sender(
        self, fact_store: FactStore, empty_episodic: EpisodicMemory,
    ) -> None:
        """The canonical subject is the casefolded form (RFC 0026 §C);
        the header tracks the storage form, not the original casing of
        ``event.sender_id``.  Pinned so a future refactor that swaps in
        the raw sender for "display" does not silently desynchronise
        the header from the row's join key.
        """
        await fact_store.store(
            subject="bob", predicate="prefers", object="tea",
            source_interaction_id="i1", asserted_at=time.time(),
        )

        mixin = _wire_mixin(
            fact_store=fact_store, episodic=empty_episodic,
            format_query="hi",
        )
        event = _make_event(sender_id="Bob", content="hi")
        await mixin._inject_memory_context(event)

        section = mixin._working_memory.get_section("facts_context")
        assert section is not None
        assert section.content.startswith("Known facts about bob:\n")
