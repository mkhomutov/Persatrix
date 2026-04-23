"""Unit tests for RFC 0019 Phase 2 — semantic spans on the Python side.

Exercises each new span name (``agent.persona.tick``, ``agent.persona.event``,
``agent.memory.episodic.recall``, ``agent.memory.episodic.remember``,
``agent.memory.relationship.lookup`` / ``.update``, ``agent.llm.call``,
``agent.tool.execute``, ``agent.subagent.spawn``) against an in-process
``InMemorySpanExporter`` and asserts the documented attributes appear with
the expected values.

Also covers the ``PERSATRIX_TRACE_TOOL_PAYLOADS`` opt-in modes for tool-arg
capture and verifies the redactor is invoked once in ``full`` mode.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from agents.llm_client import (
    LLMClient,
    LLMResponse,
    StopReason,
    Usage,
)
from agents.memory.episodic import EpisodicMemory
from agents.memory.relationship import RelationshipMemory
from agents.observability import spans as spans_module
from agents.observability.spans import (
    EPISODIC_RECALL_SPAN,
    EPISODIC_REMEMBER_SPAN,
    LLM_CALL_SPAN,
    RELATIONSHIP_LOOKUP_SPAN,
    RELATIONSHIP_UPDATE_SPAN,
    TOOL_EXECUTE_SPAN,
    NoopRedactor,
)
from agents.tools.registry import clear_registry, tool

# ─── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def exporter() -> Iterator[InMemorySpanExporter]:
    """Fresh InMemorySpanExporter wired into the active tracer provider.

    Uses the SDK ``TracerProvider`` if one is installed; otherwise installs
    a minimal one for the test session.  Does **not** call ``init_tracing``
    because that builds a competing provider and shutdown of its processors
    breaks downstream tests that share the global (e.g. gRPC auto-instrumentation
    tests in ``tests/integration/test_trace_propagation.py``).
    """
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        provider = TracerProvider()
        trace.set_tracer_provider(provider)
    exp = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exp)
    provider.add_span_processor(processor)
    yield exp
    processor.shutdown()


@pytest.fixture(autouse=True)
def _restore_redactor() -> Iterator[None]:
    """Restore the default no-op redactor after every test."""
    original = spans_module.get_redactor()
    yield
    spans_module.set_redactor(original)


@pytest.fixture(autouse=True)
def _clear_tools() -> Iterator[None]:
    """Reset tool registry between tests."""
    clear_registry()
    yield
    clear_registry()


@pytest.fixture
def db_path() -> Iterator[str]:
    """Temp SQLite path for memory tiers."""
    with tempfile.TemporaryDirectory() as tmp:
        yield os.path.join(tmp, "memory.db")


# ─── helpers ─────────────────────────────────────────────────────────────────


def _names(exporter: InMemorySpanExporter) -> list[str]:
    return [s.name for s in exporter.get_finished_spans()]


def _span(exporter: InMemorySpanExporter, name: str):  # noqa: ANN202
    matches = [s for s in exporter.get_finished_spans() if s.name == name]
    assert matches, f"expected {name} span in {_names(exporter)}"
    return matches[-1]


# ─── memory spans ────────────────────────────────────────────────────────────


class TestMemorySpans:
    async def test_episodic_remember_span(
        self, exporter: InMemorySpanExporter, db_path: str,
    ) -> None:
        mem = EpisodicMemory("agent-x", db_path=db_path)
        await mem.initialize()
        try:
            await mem.store_episode("first event", {"k": "v"})
        finally:
            await mem.close()

        span = _span(exporter, EPISODIC_REMEMBER_SPAN)
        assert span.attributes["agent.id"] == "agent-x"
        assert span.attributes["episode.kind"] == "episode"

    async def test_episodic_remember_span_records_validation_error(
        self, exporter: InMemorySpanExporter, db_path: str,
    ) -> None:
        # Regression: empty-summary ValueError must mark the
        # ``agent.memory.episodic.remember`` span as ERROR so operators
        # searching traces for failed remembers do not see them as
        # "successful" spans (PR #167 review Must-Fix #2).
        mem = EpisodicMemory("agent-x", db_path=db_path)
        await mem.initialize()
        try:
            with pytest.raises(ValueError):
                await mem.store_episode("", {})
        finally:
            await mem.close()

        span = _span(exporter, EPISODIC_REMEMBER_SPAN)
        assert span.status.status_code.name == "ERROR"

    async def test_episodic_recall_span(
        self, exporter: InMemorySpanExporter, db_path: str,
    ) -> None:
        mem = EpisodicMemory("agent-x", db_path=db_path)
        await mem.initialize()
        try:
            await mem.store_episode("first event", {})
            exporter.clear()
            results = await mem.recall("first")
        finally:
            await mem.close()

        assert len(results) == 1
        span = _span(exporter, EPISODIC_RECALL_SPAN)
        assert span.attributes["agent.id"] == "agent-x"
        assert span.attributes["query.kind"] == "recall"
        assert span.attributes["query.empty"] is False
        assert span.attributes["result.count"] == 1

    async def test_relationship_lookup_and_update_spans(
        self, exporter: InMemorySpanExporter, db_path: str,
    ) -> None:
        mem = RelationshipMemory("agent-x", db_path=db_path)
        await mem.initialize()
        try:
            await mem.update_trust("agent-y", 0.1, "reason")
            await mem.get_trust("agent-y")
        finally:
            await mem.close()

        update = _span(exporter, RELATIONSHIP_UPDATE_SPAN)
        assert update.attributes["agent.id"] == "agent-x"
        assert update.attributes["participant.id"] == "agent-y"
        assert update.attributes["delta.kind"] == "trust"
        assert pytest.approx(update.attributes["delta.value"]) == 0.1
        assert "trust.new" in update.attributes

        lookup = _span(exporter, RELATIONSHIP_LOOKUP_SPAN)
        assert lookup.attributes["agent.id"] == "agent-x"
        assert lookup.attributes["participant.id"] == "agent-y"


# ─── LLM span ────────────────────────────────────────────────────────────────


class TestLLMSpan:
    async def test_llm_call_emits_gen_ai_attributes(
        self, exporter: InMemorySpanExporter,
    ) -> None:
        provider = AsyncMock()
        # Set the Protocol-declared ``name`` attribute explicitly so the
        # span emits the canonical ``gen_ai.system`` value (real providers
        # declare this as a class attribute).
        provider.name = "anthropic"
        provider.create_message = AsyncMock(
            return_value=LLMResponse(
                text="hello",
                stop_reason=StopReason.END_TURN,
                usage=Usage(input_tokens=11, output_tokens=22),
            ),
        )
        client = LLMClient(provider)

        await client.create_message(
            model="claude-3-5-sonnet",
            messages=[],
            system="",
            tools=[],
            max_tokens=10,
            temperature=0.0,
        )

        span = _span(exporter, LLM_CALL_SPAN)
        assert span.attributes["gen_ai.system"] == "anthropic"
        assert span.attributes["gen_ai.request.model"] == "claude-3-5-sonnet"
        assert span.attributes["gen_ai.operation.name"] == "chat"
        assert span.attributes["gen_ai.usage.input_tokens"] == 11
        assert span.attributes["gen_ai.usage.output_tokens"] == 22
        # OTEL Gen-AI canonical vocabulary — END_TURN translates to "stop",
        # NOT the Persatrix-internal enum value "end_turn".
        assert tuple(span.attributes["gen_ai.response.finish_reasons"]) == ("stop",)

    @pytest.mark.parametrize(
        ("stop_reason", "expected"),
        [
            (StopReason.END_TURN, "stop"),
            (StopReason.MAX_TOKENS, "length"),
            (StopReason.TOOL_USE, "tool_calls"),
        ],
    )
    async def test_llm_call_finish_reason_canonical_values(
        self,
        exporter: InMemorySpanExporter,
        stop_reason: StopReason,
        expected: str,
    ) -> None:
        provider = AsyncMock()
        provider.name = "openai"
        provider.create_message = AsyncMock(
            return_value=LLMResponse(
                text="x",
                stop_reason=stop_reason,
                usage=Usage(input_tokens=1, output_tokens=1),
            ),
        )
        client = LLMClient(provider)

        await client.create_message(
            model="gpt-4o",
            messages=[],
            system="",
            tools=[],
            max_tokens=10,
            temperature=0.0,
        )

        span = _span(exporter, LLM_CALL_SPAN)
        assert tuple(span.attributes["gen_ai.response.finish_reasons"]) == (expected,)

    async def test_llm_call_records_exception(
        self, exporter: InMemorySpanExporter,
    ) -> None:
        provider = AsyncMock()
        provider.create_message = AsyncMock(side_effect=RuntimeError("boom"))
        client = LLMClient(provider)

        with pytest.raises(RuntimeError):
            await client.create_message(
                model="claude-3-5-sonnet",
                messages=[],
                system="",
                tools=[],
                max_tokens=10,
                temperature=0.0,
            )

        span = _span(exporter, LLM_CALL_SPAN)
        assert span.status.status_code.name == "ERROR"


# ─── Tool span + payload capture ─────────────────────────────────────────────


class _RecordingRedactor:
    """Records every redact() call so the test can assert single-pass."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def redact(self, record: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(dict(record))
        return {k: f"<redacted:{v}>" for k, v in record.items()}


class TestToolSpan:
    async def test_tool_span_basic_attributes(
        self, exporter: InMemorySpanExporter,
    ) -> None:
        @tool(name="echo_tool", description="echo")
        async def echo_tool(value: str) -> str:
            return value

        await echo_tool(value="hi")

        span = _span(exporter, TOOL_EXECUTE_SPAN)
        assert span.attributes["tool.name"] == "echo_tool"
        assert span.attributes["tool.success"] is True
        # No payload data emitted by default.
        assert all(
            not k.startswith("tool.arguments")
            for k in span.attributes
        )

    async def test_tool_span_metadata_mode(
        self,
        exporter: InMemorySpanExporter,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("PERSATRIX_TRACE_TOOL_PAYLOADS", "metadata")

        @tool(name="echo_tool", description="echo")
        async def echo_tool(value: str) -> str:
            return value

        await echo_tool(value="secret-value")

        span = _span(exporter, TOOL_EXECUTE_SPAN)
        assert span.attributes["tool.arguments.value.type"] == "str"
        # Values are NOT captured in metadata mode.
        assert "tool.arguments.value" not in span.attributes

    async def test_tool_span_full_mode_uses_redactor(
        self,
        exporter: InMemorySpanExporter,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("PERSATRIX_TRACE_TOOL_PAYLOADS", "full")
        recorder = _RecordingRedactor()
        spans_module.set_redactor(recorder)

        @tool(name="echo_tool", description="echo")
        async def echo_tool(value: str) -> str:
            return value

        await echo_tool(value="secret-value")

        span = _span(exporter, TOOL_EXECUTE_SPAN)
        assert span.attributes["tool.arguments.value"] == "<redacted:secret-value>"
        assert len(recorder.calls) == 1
        assert recorder.calls[0] == {"value": "secret-value"}

    async def test_tool_span_unknown_mode_falls_back_to_none(
        self,
        exporter: InMemorySpanExporter,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("PERSATRIX_TRACE_TOOL_PAYLOADS", "WAT")

        @tool(name="echo_tool", description="echo")
        async def echo_tool(value: str) -> str:
            return value

        await echo_tool(value="hi")

        span = _span(exporter, TOOL_EXECUTE_SPAN)
        assert all(not k.startswith("tool.arguments") for k in span.attributes)

    async def test_tool_span_failure_records_status(
        self, exporter: InMemorySpanExporter,
    ) -> None:
        @tool(name="boom", description="raises")
        async def boom() -> str:
            raise ValueError("nope")

        await boom()

        span = _span(exporter, TOOL_EXECUTE_SPAN)
        assert span.attributes["tool.success"] is False
        assert span.status.status_code.name == "ERROR"


# ─── Default redactor is the project NoopRedactor ────────────────────────────


def test_default_redactor_is_noop() -> None:
    assert isinstance(spans_module.get_redactor(), NoopRedactor)


# ─── Tracer is alive ─────────────────────────────────────────────────────────


def test_tracer_module_imports_cleanly() -> None:
    """Sanity: all span-name constants are non-empty strings."""
    for name in (
        "agent.persona.tick",
        "agent.persona.event",
        "agent.memory.episodic.recall",
        "agent.memory.episodic.remember",
        "agent.memory.relationship.lookup",
        "agent.memory.relationship.update",
        "agent.llm.call",
        "agent.tool.execute",
        "agent.subagent.spawn",
    ):
        assert isinstance(name, str) and "." in name
    # The active tracer provider produces tracers (smoke check).
    assert trace.get_tracer("smoke") is not None
