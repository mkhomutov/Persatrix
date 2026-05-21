"""RFC 0024 PR 3a — memory-tier write-side emission of ``MemoryWriteEvent``.

Pins the contracts named in the RFC 0024 PR plan:

* Every memory tier emits **exactly one** ``MemoryWriteEvent`` on successful
  write.  A *failed* write emits nothing.
* Episodic appends score salience ``0.0`` (conservative — RFC §D).
* ``REFLECTION_CONTRADICTION_SALIENCE`` is the named constant the
  calibration follow-up flips; PR 3b's threshold default (``0.95``) is
  strictly above it so salience wakes stay off by construction.
* ``source_span_id`` is populated when an outer OTEL span is active at
  write call-site, and ``None`` when no span is active.  The outer span
  is the loop-back guard's input in PR 3b.

PR 3a ships **no subscriber**; PR 3b adds the ``EventLoop`` subscriber.
These tests install a transient subscriber via the global bus accessor.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Iterator

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from agents.memory._events import (
    MemoryWriteBus,
    MemoryWriteEvent,
    get_memory_write_bus,
    set_memory_write_bus,
)
from agents.memory._salience import REFLECTION_CONTRADICTION_SALIENCE
from agents.memory.episodic import EpisodicMemory
from agents.memory.facts import FactStore
from agents.memory.relationship import RelationshipMemory
from agents.observability.spans import current_llm_span_id

# ─── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _real_tracer_provider() -> None:
    """Install an SDK ``TracerProvider`` so spans report valid span_ids.

    The OTEL API's default provider returns ``INVALID_SPAN`` (span_id=0)
    which would make ``current_llm_span_id()`` always return ``None``.
    Tests that pin the populated-span case need a real SDK provider.
    Idempotent — does not replace an already-installed SDK provider.
    """
    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        trace.set_tracer_provider(TracerProvider())


@pytest.fixture
def fresh_bus() -> Iterator[MemoryWriteBus]:
    """Install a fresh global ``MemoryWriteBus`` per test (no cross-test bleed)."""
    original = get_memory_write_bus()
    bus = MemoryWriteBus()
    set_memory_write_bus(bus)
    try:
        yield bus
    finally:
        set_memory_write_bus(original)


@pytest.fixture
async def episodic() -> AsyncIterator[EpisodicMemory]:
    mem = EpisodicMemory(agent_id="agent-a", db_path=":memory:")
    await mem.initialize()
    try:
        yield mem
    finally:
        await mem.close()


@pytest.fixture
async def facts() -> AsyncIterator[FactStore]:
    store = FactStore(agent_id="agent-a", db_path=":memory:")
    await store.initialize()
    try:
        yield store
    finally:
        await store.close()


@pytest.fixture
async def relationship() -> AsyncIterator[RelationshipMemory]:
    mem = RelationshipMemory(agent_id="agent-a", db_path=":memory:")
    await mem.initialize()
    try:
        yield mem
    finally:
        await mem.close()


# ─── Named scoring constant ─────────────────────────────────────────────────


class TestReflectionContradictionConstant:
    """``REFLECTION_CONTRADICTION_SALIENCE`` is the calibration knob.

    Pinned here so PR 3b's ``test_event_loop_salience_default_off`` and any
    future tuning follow-up have a single named place to flip.  The value
    MUST stay strictly below PR 3b's threshold default (0.95) — that
    inequality is the default-off invariant.
    """

    def test_constant_is_in_unit_interval(self) -> None:
        assert 0.0 < REFLECTION_CONTRADICTION_SALIENCE <= 1.0

    def test_constant_matches_rfc0024_pr_plan_value(self) -> None:
        # RFC 0024 PR plan §PR 3a: reflection contradictions score 0.6.
        assert REFLECTION_CONTRADICTION_SALIENCE == 0.6


# ─── current_llm_span_id ────────────────────────────────────────────────────


class TestCurrentLlmSpanId:
    def test_returns_none_when_no_active_span(self) -> None:
        assert current_llm_span_id() is None

    def test_returns_hex_span_id_when_span_active(self) -> None:
        tracer = trace.get_tracer("test")
        with tracer.start_as_current_span("test-llm-span") as span:
            sid = current_llm_span_id()
        assert sid is not None
        # Lower-case, zero-padded 16-char hex per OTEL span_id convention.
        assert len(sid) == 16
        assert sid == f"{span.get_span_context().span_id:016x}"


# ─── Episodic tier ──────────────────────────────────────────────────────────


class TestEpisodicEmission:
    async def test_store_episode_emits_event_with_salience_zero(
        self, episodic: EpisodicMemory, fresh_bus: MemoryWriteBus,
    ) -> None:
        seen: list[MemoryWriteEvent] = []
        fresh_bus.subscribe(seen.append)

        await episodic.store_episode("summary one", {"k": "v"})

        assert len(seen) == 1
        ev = seen[0]
        assert ev.tier == "episodic"
        assert ev.agent_id == "agent-a"
        # RFC §D: episodic appends are conservative — salience 0.0.
        assert ev.salience == 0.0
        assert ev.written_at > 0.0

    async def test_failed_store_episode_emits_nothing(
        self, episodic: EpisodicMemory, fresh_bus: MemoryWriteBus,
    ) -> None:
        seen: list[MemoryWriteEvent] = []
        fresh_bus.subscribe(seen.append)

        with pytest.raises(ValueError):
            # Empty summary fails validation before insert.
            await episodic.store_episode("", {})

        assert seen == [], "Failed write must not emit a MemoryWriteEvent"

    async def test_store_episode_captures_outer_span_id(
        self, episodic: EpisodicMemory, fresh_bus: MemoryWriteBus,
    ) -> None:
        """When an outer span is active (e.g. the LLM-call span in the
        agent's action loop), the emitted event's ``source_span_id`` is the
        outer span's id — the load-bearing input to PR 3b's loop-back guard.
        """
        seen: list[MemoryWriteEvent] = []
        fresh_bus.subscribe(seen.append)

        tracer = trace.get_tracer("test")
        with tracer.start_as_current_span("outer-llm-like-span") as outer:
            await episodic.store_episode("summary", {})
            outer_sid_hex = f"{outer.get_span_context().span_id:016x}"

        assert len(seen) == 1
        assert seen[0].source_span_id == outer_sid_hex

    async def test_store_episode_source_span_id_none_when_no_outer_span(
        self, episodic: EpisodicMemory, fresh_bus: MemoryWriteBus,
    ) -> None:
        """With no outer span, ``source_span_id`` is ``None``.

        The episodic write's own internal ``EPISODIC_REMEMBER_SPAN`` must
        have popped by emission time so that we don't leak a child-span id
        as the loop-back guard's signal.
        """
        seen: list[MemoryWriteEvent] = []
        fresh_bus.subscribe(seen.append)

        await episodic.store_episode("summary", {})

        assert len(seen) == 1
        assert seen[0].source_span_id is None


# ─── Notes tier ─────────────────────────────────────────────────────────────


class TestNotesEmission:
    async def test_store_note_emits_event_with_salience_zero(
        self, episodic: EpisodicMemory, fresh_bus: MemoryWriteBus,
    ) -> None:
        seen: list[MemoryWriteEvent] = []
        fresh_bus.subscribe(seen.append)

        await episodic.store_note(topic="t", content="c")

        assert len(seen) == 1
        ev = seen[0]
        assert ev.tier == "notes"
        assert ev.agent_id == "agent-a"
        assert ev.salience == 0.0

    async def test_failed_store_note_emits_nothing(
        self, episodic: EpisodicMemory, fresh_bus: MemoryWriteBus,
    ) -> None:
        seen: list[MemoryWriteEvent] = []
        fresh_bus.subscribe(seen.append)

        with pytest.raises(ValueError):
            await episodic.store_note(topic="", content="c")

        assert seen == []


# ─── Facts tier ─────────────────────────────────────────────────────────────


class TestFactsEmission:
    async def test_store_emits_event_with_salience_zero(
        self, facts: FactStore, fresh_bus: MemoryWriteBus,
    ) -> None:
        seen: list[MemoryWriteEvent] = []
        fresh_bus.subscribe(seen.append)

        await facts.store(
            subject="bob",
            predicate="prefers",  # RFC 0026 §B allowlist member
            object="tea",
            source_interaction_id=None,
            asserted_at=1000.0,
        )

        assert len(seen) == 1
        ev = seen[0]
        assert ev.tier == "facts"
        assert ev.agent_id == "agent-a"
        assert ev.salience == 0.0

    async def test_failed_store_emits_nothing(
        self, facts: FactStore, fresh_bus: MemoryWriteBus,
    ) -> None:
        seen: list[MemoryWriteEvent] = []
        fresh_bus.subscribe(seen.append)

        with pytest.raises(ValueError):
            await facts.store(
                subject="",  # empty subject fails validation
                predicate="likes",
                object="tea",
                source_interaction_id=None,
                asserted_at=1.0,
            )

        assert seen == []


# ─── Relationship tier ──────────────────────────────────────────────────────


class TestRelationshipEmission:
    async def test_record_interaction_emits_event_with_salience_zero(
        self, relationship: RelationshipMemory, fresh_bus: MemoryWriteBus,
    ) -> None:
        seen: list[MemoryWriteEvent] = []
        fresh_bus.subscribe(seen.append)

        await relationship.record_interaction(
            other_id="bob", interaction_type="chat",
        )

        assert len(seen) == 1
        ev = seen[0]
        assert ev.tier == "relationship"
        assert ev.agent_id == "agent-a"
        assert ev.salience == 0.0

    async def test_failed_record_interaction_emits_nothing(
        self, relationship: RelationshipMemory, fresh_bus: MemoryWriteBus,
    ) -> None:
        seen: list[MemoryWriteEvent] = []
        fresh_bus.subscribe(seen.append)

        with contextlib.suppress(ValueError):
            await relationship.record_interaction(
                other_id="bob", interaction_type="",  # empty type fails
            )

        assert seen == []
