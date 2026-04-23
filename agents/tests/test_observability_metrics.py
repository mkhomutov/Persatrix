"""Tests for :mod:`agents.observability.metrics` (RFC 0019 PR 3)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast

import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agents.observability import metrics as pmetrics

_TOOL_A: dict[str, Any] = {"agent.id": "t", "tool.name": "x", "tool.success": True}
_LLM_CALL_A: dict[str, Any] = {
    "agent.id": "t",
    "gen_ai.system": "s",
    "gen_ai.request.model": "m",
    "persatrix.llm.cache.hit": False,
}
_LLM_TOK_A: dict[str, Any] = {
    "agent.id": "t",
    "gen_ai.request.model": "m",
    "gen_ai.token.type": "input",
}
_LLM_DUR_A: dict[str, Any] = {"agent.id": "t", "gen_ai.request.model": "m"}
_EVENT_A: dict[str, Any] = {"agent.id": "t", "event.type": "tick"}
_TICK_A: dict[str, Any] = {"agent.id": "t"}
_DROP_A: dict[str, Any] = {"agent.id": "t", "reason": "queue_full"}


@pytest.fixture
def metric_reader() -> Iterator[InMemoryMetricReader]:
    reader = InMemoryMetricReader()
    pmetrics.init_metrics(reader=reader)
    try:
        yield reader
    finally:
        if pmetrics._provider is not None:
            pmetrics._provider.force_flush()
            pmetrics._provider.shutdown()
            pmetrics._provider = None
            pmetrics._instruments = None


def _touch_all(inst: pmetrics._Instruments) -> None:
    inst.tool_invocations.add(1, attributes=_TOOL_A)
    inst.tool_duration.record(1.0, attributes=_TOOL_A)
    inst.llm_calls.add(1, attributes=_LLM_CALL_A)
    inst.llm_tokens.add(1, attributes=_LLM_TOK_A)
    inst.llm_duration.record(1.0, attributes=_LLM_DUR_A)
    inst.event_dispatched.add(1, attributes=_EVENT_A)
    inst.persona_tick_interval.record(1.0, attributes=_TICK_A)
    inst.agent_active.add(0)
    inst.spans_dropped.add(1, attributes=_DROP_A)
    inst.logs_dropped.add(1, attributes=_DROP_A)


def _collect(reader: InMemoryMetricReader) -> dict[str, Any]:
    data = reader.get_metrics_data()
    out: dict[str, Any] = {}
    if data is None:
        return out
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                out[m.name] = m
    return out


class TestInstrumentInventory:
    def test_all_documented_instruments_registered(
        self, metric_reader: InMemoryMetricReader,
    ) -> None:
        _touch_all(pmetrics.get_instruments())
        names = set(_collect(metric_reader).keys())
        expected = {
            "agent.tool.invocations",
            "agent.tool.duration",
            "agent.llm.calls",
            "agent.llm.tokens",
            "agent.llm.duration",
            "agent.event.dispatched",
            "agent.persona.tick.interval",
            "agent.active",
            "agent.observability.spans.dropped",
            "agent.observability.logs.dropped",
        }
        missing = expected - names
        assert not missing, f"Missing instruments: {missing}"

    def test_units_match_rfc_table(self, metric_reader: InMemoryMetricReader) -> None:
        _touch_all(pmetrics.get_instruments())
        seen = {name: m.unit for name, m in _collect(metric_reader).items()}
        expected_units: dict[str, str] = {
            "agent.tool.invocations": "{invocation}",
            "agent.tool.duration": "ms",
            "agent.llm.calls": "{call}",
            "agent.llm.tokens": "{token}",
            "agent.llm.duration": "ms",
            "agent.event.dispatched": "{event}",
            "agent.persona.tick.interval": "ms",
            "agent.observability.spans.dropped": "{span}",
            "agent.observability.logs.dropped": "{record}",
        }
        for name, unit in expected_units.items():
            assert seen.get(name) == unit, f"{name} unit={seen.get(name)!r} expected={unit!r}"


class TestCounterMonotonicity:
    def test_counter_is_monotonic(self, metric_reader: InMemoryMetricReader) -> None:
        inst = pmetrics.get_instruments()
        for _ in range(3):
            inst.event_dispatched.add(1, attributes=_EVENT_A)
        m = _collect(metric_reader).get("agent.event.dispatched")
        assert m is not None
        total = sum(cast("int", getattr(dp, "value", 0)) for dp in m.data.data_points)
        assert total >= 3


class TestHistogramExemplars:
    def test_histogram_records_under_active_span(self, metric_reader: InMemoryMetricReader) -> None:
        tp = TracerProvider()
        exporter = InMemorySpanExporter()
        tp.add_span_processor(SimpleSpanProcessor(exporter))
        tracer = tp.get_tracer("test")

        inst = pmetrics.get_instruments()
        with tracer.start_as_current_span("t") as span:
            span_ctx = span.get_span_context()
            inst.llm_duration.record(123.0, attributes=_LLM_DUR_A)

        m = _collect(metric_reader).get("agent.llm.duration")
        assert m is not None
        for dp in m.data.data_points:
            exemplars = getattr(dp, "exemplars", None)
            if not exemplars:
                continue
            for ex in exemplars:
                assert ex.trace_id == span_ctx.trace_id
                assert ex.span_id == span_ctx.span_id


class TestInitIdempotent:
    def test_shutdown_clears_module_state(self, metric_reader: InMemoryMetricReader) -> None:
        assert pmetrics._provider is not None
        assert pmetrics._instruments is not None
        pmetrics._provider.force_flush()
        pmetrics._provider.shutdown()
        pmetrics._provider = None
        pmetrics._instruments = None
        with pytest.raises(RuntimeError):
            pmetrics.get_instruments()
        assert pmetrics.try_get_instruments() is None


class TestAttributeHelpers:
    def test_tool_attrs_shape(self) -> None:
        a = pmetrics.tool_attrs(agent_id="x", tool_name="y", success=True)
        assert a == {"agent.id": "x", "tool.name": "y", "tool.success": True}

    def test_llm_token_attrs_literal(self) -> None:
        a = pmetrics.llm_token_attrs(agent_id="x", request_model="m", token_type="input")
        assert a["gen_ai.token.type"] == "input"
