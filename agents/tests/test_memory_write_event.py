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
import logging
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
from agents.memory._facts_audit import emit_audit
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

    async def test_store_note_captures_outer_span_id(
        self, episodic: EpisodicMemory, fresh_bus: MemoryWriteBus,
    ) -> None:
        """Notes have no inner span (unlike episodic's
        ``EPISODIC_REMEMBER_SPAN``), so the captured ``source_span_id``
        is just the outer span.  Pinning it here so a future refactor
        that wraps :meth:`store_note` in an inner span surfaces in CI —
        otherwise PR 3b's loop-back guard would see a child span id
        instead of the LLM-call ancestor."""
        seen: list[MemoryWriteEvent] = []
        fresh_bus.subscribe(seen.append)

        tracer = trace.get_tracer("test")
        with tracer.start_as_current_span("outer-llm-like-span") as outer:
            await episodic.store_note(topic="t", content="c")
            outer_sid_hex = f"{outer.get_span_context().span_id:016x}"

        assert len(seen) == 1
        assert seen[0].source_span_id == outer_sid_hex


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

    async def test_store_captures_outer_span_id_through_audit_piggyback(
        self, facts: FactStore, fresh_bus: MemoryWriteBus,
    ) -> None:
        """The facts tier emits via the ``_emit_audit("fact.store", …)``
        piggyback in :mod:`._facts_audit` rather than a direct
        ``emit_for_tier`` call.  Pin that the piggyback path still
        captures the outer-span id correctly — otherwise the indirection
        could mask a regression where PR 3b's loop-back guard receives
        ``None`` for fact-tier writes performed inside an LLM-call span.
        """
        seen: list[MemoryWriteEvent] = []
        fresh_bus.subscribe(seen.append)

        tracer = trace.get_tracer("test")
        with tracer.start_as_current_span("outer-llm-like-span") as outer:
            await facts.store(
                subject="bob", predicate="prefers", object="tea",
                source_interaction_id=None, asserted_at=1000.0,
            )
            outer_sid_hex = f"{outer.get_span_context().span_id:016x}"

        assert len(seen) == 1
        assert seen[0].source_span_id == outer_sid_hex


# ─── Facts piggyback contract (observability of the silent-drop branch) ────


class TestFactsAuditPiggybackContract:
    """The facts tier piggybacks ``MemoryWriteEvent`` emission on the
    ``_emit_audit("fact.store", …)`` call inside :mod:`._facts_audit`.

    The current production caller (``FactStore.store``) always passes
    ``agent_id=self._agent_id``; the piggyback gate is
    ``isinstance(fields.get("agent_id"), str)``.  Without observability
    on the gate's miss branch, a future caller that forgets ``agent_id=``
    would silently break PR 3b's salience-wake coverage for the facts
    tier with no signal in the logs.  These tests pin a WARNING at the
    miss branch so the contract violation is loud, while keeping the
    write-path failure-isolation contract intact (we still must NOT
    raise — the row is already committed by the time we get here).
    """

    def test_fact_store_audit_without_agent_id_logs_warning(
        self, fresh_bus: MemoryWriteBus, caplog: pytest.LogCaptureFixture,
    ) -> None:
        seen: list[MemoryWriteEvent] = []
        fresh_bus.subscribe(seen.append)

        with caplog.at_level(logging.WARNING, logger="agents.memory.facts"):
            # Programmer error: ``agent_id`` missing entirely.
            emit_audit("fact.store", fact_id="f1", subject="s")

        assert seen == [], (
            "Memory-write event must not fire without a valid agent_id"
        )
        assert any(
            "agent_id" in rec.message and rec.levelno == logging.WARNING
            for rec in caplog.records
        ), "Missing agent_id on fact.store must surface as a WARNING"

    def test_fact_store_audit_with_non_string_agent_id_logs_warning(
        self, fresh_bus: MemoryWriteBus, caplog: pytest.LogCaptureFixture,
    ) -> None:
        seen: list[MemoryWriteEvent] = []
        fresh_bus.subscribe(seen.append)

        with caplog.at_level(logging.WARNING, logger="agents.memory.facts"):
            emit_audit("fact.store", agent_id=42, fact_id="f1")

        assert seen == []
        assert any(
            "agent_id" in rec.message and rec.levelno == logging.WARNING
            for rec in caplog.records
        )

    def test_fact_store_audit_with_valid_agent_id_emits_no_warning(
        self, fresh_bus: MemoryWriteBus, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Happy path regression guard: a well-formed fact.store audit
        must NOT produce the missing-agent_id warning and MUST emit
        exactly one memory-write event."""
        seen: list[MemoryWriteEvent] = []
        fresh_bus.subscribe(seen.append)

        with caplog.at_level(logging.WARNING, logger="agents.memory.facts"):
            emit_audit("fact.store", agent_id="agent-a", fact_id="f1")

        assert len(seen) == 1
        assert seen[0].tier == "facts"
        assert seen[0].agent_id == "agent-a"
        assert not any(
            "agent_id" in rec.message and rec.levelno == logging.WARNING
            for rec in caplog.records
        ), "Valid agent_id must not trigger the missing-agent_id warning"

    def test_non_fact_store_audit_does_not_warn_on_missing_agent_id(
        self, fresh_bus: MemoryWriteBus, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Only ``fact.store`` piggybacks emission; other audit events
        (``fact.recalled``, ``fact.supersede``, …) intentionally do not
        emit ``MemoryWriteEvent`` and therefore must not warn on
        missing ``agent_id`` either — the gate applies only to the one
        event that carries the piggyback contract."""
        seen: list[MemoryWriteEvent] = []
        fresh_bus.subscribe(seen.append)

        with caplog.at_level(logging.WARNING, logger="agents.memory.facts"):
            emit_audit("fact.recalled", fact_ids=["f1"])
            emit_audit("fact.supersede", superseded_fact_id="f1", by_fact_id="f2")

        assert seen == [], "Non-fact.store audit must not emit MemoryWriteEvent"
        assert not any(
            "agent_id" in rec.message and rec.levelno == logging.WARNING
            for rec in caplog.records
        )


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

    async def test_record_interaction_captures_outer_span_id(
        self, relationship: RelationshipMemory, fresh_bus: MemoryWriteBus,
    ) -> None:
        """Relationship has no inner span at v0.3.3; pin the captured
        ``source_span_id`` so a future refactor that adds one to
        :meth:`record_interaction` surfaces in CI rather than silently
        regressing PR 3b's loop-back guard input."""
        seen: list[MemoryWriteEvent] = []
        fresh_bus.subscribe(seen.append)

        tracer = trace.get_tracer("test")
        with tracer.start_as_current_span("outer-llm-like-span") as outer:
            await relationship.record_interaction(
                other_id="bob", interaction_type="chat",
            )
            outer_sid_hex = f"{outer.get_span_context().span_id:016x}"

        assert len(seen) == 1
        assert seen[0].source_span_id == outer_sid_hex
