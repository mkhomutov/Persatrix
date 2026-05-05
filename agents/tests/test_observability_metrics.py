"""Tests for :mod:`agents.observability.metrics` (RFC 0019 PR 3)."""

from __future__ import annotations

import asyncio
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
        # Use the public ``shutdown()`` API instead of poking ``_provider``
        # directly so the fixture stays stable across SDK upgrades that may
        # rename the private state (PR #170 review nice-to-have).
        asyncio.run(pmetrics.shutdown())


def _touch_all(inst: pmetrics._Instruments) -> None:
    inst.tool_invocations.add(1, attributes=_TOOL_A)
    inst.tool_duration.record(1.0, attributes=_TOOL_A)
    inst.llm_calls.add(1, attributes=_LLM_CALL_A)
    inst.llm_tokens.add(1, attributes=_LLM_TOK_A)
    inst.llm_duration.record(1.0, attributes=_LLM_DUR_A)
    inst.event_dispatched.add(1, attributes=_EVENT_A)
    inst.persona_tick_interval.record(1.0, attributes=_TICK_A)
    # PR-170 M2: ``agent.active`` is exercised with a real ``+1 / -1``
    # round-trip in :class:`TestAgentActiveLifecycle` below, not a no-op
    # ``add(0)`` here.  The previous touch-with-zero existed solely to
    # satisfy the inventory test and masked the fact that no production
    # code path was incrementing the gauge.
    inst.agent_active.add(1, attributes={"agent.id": "t"})
    inst.agent_active.add(-1, attributes={"agent.id": "t"})
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

        # PR-170 S4: previous shape iterated ``dp.exemplars`` and asserted
        # inside the inner loop, which passed vacuously when the SDK
        # version-/aggregation-config combo emitted zero exemplars.  Collect
        # all exemplars across all data points first; require at least one,
        # and require it to carry the active span's trace/span IDs.  This
        # regression-detects future SDK behaviour changes that would
        # silently drop exemplars instead of pretending the test still
        # exercises the contract.
        all_exemplars = [
            ex
            for dp in m.data.data_points
            for ex in (getattr(dp, "exemplars", None) or [])
        ]
        assert all_exemplars, (
            "expected at least one exemplar on agent.llm.duration; "
            "OTEL SDK emitted none under an active span"
        )
        assert any(
            ex.trace_id == span_ctx.trace_id and ex.span_id == span_ctx.span_id
            for ex in all_exemplars
        ), "no exemplar matched the active span context"


class TestAgentActiveLifecycle:
    """PR-170 M2 regression: ``agent.active`` must round-trip to zero.

    The instrument is wired in :func:`agents.server.main` (``+1`` after
    ``load_agent``, ``-1`` in the teardown path of ``_run``).  This test
    drives the helper directly rather than spawning a real gRPC server so
    it stays a unit test; it asserts the only contract that matters for
    dashboards: a clean shutdown leaves the gauge at the same value it had
    at startup.
    """

    def test_round_trip_returns_to_zero(
        self, metric_reader: InMemoryMetricReader,
    ) -> None:
        inst = pmetrics.get_instruments()
        attrs = {"agent.id": "demo"}
        inst.agent_active.add(1, attributes=attrs)
        inst.agent_active.add(-1, attributes=attrs)

        m = _collect(metric_reader).get("agent.active")
        assert m is not None
        # UpDownCounter sum aggregation: across both deltas the cumulative
        # value for the ``demo`` agent must be 0.  Sum across data points so
        # the test is independent of how the SDK partitions points.
        total = sum(
            cast("int", getattr(dp, "value", 0))
            for dp in m.data.data_points
            if dict(dp.attributes).get("agent.id") == "demo"
        )
        assert total == 0


class TestInitIdempotent:
    def test_shutdown_clears_module_state(self, metric_reader: InMemoryMetricReader) -> None:
        # Pre-condition: fixture already ran ``init_metrics()``.
        assert pmetrics.try_get_instruments() is not None
        # Use the public ``shutdown()`` instead of poking ``_provider`` /
        # ``_instruments`` directly (PR #170 review nice-to-have).  The
        # contract being asserted: a successful shutdown leaves
        # ``try_get_instruments()`` returning ``None`` and
        # ``get_instruments()`` raising.
        asyncio.run(pmetrics.shutdown())
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

    def test_gate_attrs_shape_matches_rfc_0011_section_d(self) -> None:
        # RFC 0011 §D explicitly specifies the ``channel.messages.gated``
        # label set as ``{channel_id, policy}`` and excludes
        # ``subscriber_id`` for cardinality reasons (members × channels ×
        # policies ~30,000 series at N=200). The agent identity is
        # carried by the OTLP resource (``service.instance.id`` set from
        # ``PERSATRIX_AGENT_ID``) so dropping the explicit per-record
        # label does not lose information — it just stops duplicating it
        # at the metric attribute layer where the cardinality cost is
        # paid.
        a = pmetrics.gate_attrs(channel_id="group:planning", policy="when_mentioned")
        assert a == {"channel_id": "group:planning", "policy": "when_mentioned"}
        assert "agent.id" not in a, (
            "RFC 0011 §D excludes subscriber_id (agent.id) from the "
            "channel.messages.gated label set; per-subscriber drill-down "
            "lives in OTEL spans, not metric attributes."
        )
