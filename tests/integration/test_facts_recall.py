"""RFC 0026 PR 3 — FactStore.recall wired into the MemoryBudget allocator.

Pins the PR 3 deliverables called out in
:doc:`docs/rfcs/0026-pr-plan.md` §PR 3: the facts tier slots between
relationship and notes in the canonical cross-RFC priority order, calls
:meth:`agents.memory.facts.FactStore.recall` for each of ``(sender,
*mentioned_entities)``, and routes admitted rows through
:class:`agents.persona_runtime.memory_budget.MemoryBudget` with a
per-tier floor.  The dementia-test core (fact in turn N injected at turn
N+1 *without* the subject string appearing in the query) is the
load-bearing leg of MT-MEMORY-005 and pinned here.

Contracts asserted:

* Subject-indexed recall round-trip on a fresh DB.
* Dementia-test core: a fact stored at interaction N is injected at
  N+1 even when the next event's natural-language query does not
  mention the subject string (the dementia test that fails today is
  the one where ``query`` is a follow-up question that does not name
  the entity).
* Tier ordering: facts admitted before notes when the budget is tight.
* ``memory.facts.enabled: false`` skips the tier entirely; no
  ``agent.facts.injected`` increment.
* RFC 0017's 1500-token allocator cap still holds with the facts tier
  wired in.
* Per-tier header tokens are charged against the budget (PR 3 review
  precedent — the prepended header is not free).
"""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from _otel_test_helpers import counter_total

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
    mixin._fact_store = fact_store  # type: ignore[attr-defined]
    mixin._facts_enabled = facts_enabled  # type: ignore[attr-defined]
    mixin._facts_budget_tokens = 200  # type: ignore[attr-defined]
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


def _build_meter() -> Any:
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader  # noqa: PLC0415

    from agents.observability import metrics as metrics_mod  # noqa: PLC0415

    reader = InMemoryMetricReader()
    metrics_mod.init_metrics(reader=reader)
    return reader, metrics_mod


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


class TestTierOrdering:
    async def test_facts_admitted_before_notes_section_added(
        self, fact_store: FactStore, empty_episodic: EpisodicMemory,
    ) -> None:
        """The five-tier order matches RFC 0027 §F end-state:
        relationship → channel_history → facts → episodic → notes.

        Pinned via the ``add_section`` call sequence; the facts
        section must be added between relationship_context and
        recent_notes.
        """
        await fact_store.store(
            subject="bob", predicate="prefers", object="tea",
            source_interaction_id="i1", asserted_at=time.time(),
        )
        await empty_episodic.store_note(
            topic="rollout", content="bob asked about rollout planning",
        )

        rel = _FakeRelSummary(
            other_participant_id="bob",
            other_participant_type="user",
            interaction_count=3,
            trust_score=0.6,
            notes="bob is detail-focused",
            last_interaction_at=time.time() - 60,
            first_interaction_at=time.time() - 3600,
        )
        mixin = _wire_mixin(
            fact_store=fact_store, episodic=empty_episodic, rel=rel,
            format_query="rollout",
        )

        order: list[str] = []
        real_add = mixin._working_memory.add_section

        def _record(section: Any) -> None:
            order.append(section.name)
            real_add(section)

        mixin._working_memory.add_section = _record  # type: ignore[assignment]

        event = _make_event(sender_id="bob", content="rollout")
        await mixin._inject_memory_context(event)

        # The facts section must sit between relationship_context and recent_notes.
        assert "facts_context" in order
        assert "relationship_context" in order
        assert "recent_notes" in order
        rel_idx = order.index("relationship_context")
        facts_idx = order.index("facts_context")
        notes_idx = order.index("recent_notes")
        assert rel_idx < facts_idx < notes_idx, (
            f"tier order drifted: {order}"
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

        reader, metrics_mod = _build_meter()
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

        reader, metrics_mod = _build_meter()
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
