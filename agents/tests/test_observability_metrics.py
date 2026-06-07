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


_WAKE_INBOUND_A: dict[str, Any] = {"agent.id": "t", "wake.kind": "inbound"}
_WAKE_SCHEDULED_A: dict[str, Any] = {
    "agent.id": "t",
    "wake.kind": "scheduled",
    "timer_id": "legacy_tick",
}
_WAKE_SALIENCE_A: dict[str, Any] = {
    "agent.id": "t",
    "wake.kind": "salience",
    "tier": "episodic",
    "suppressed_reason": "none",
}
_WAKE_DROPPED_A: dict[str, Any] = {"agent.id": "t", "wake.kind": "dropped"}


def _touch_all(inst: pmetrics._Instruments) -> None:
    inst.tool_invocations.add(1, attributes=_TOOL_A)
    inst.tool_duration.record(1.0, attributes=_TOOL_A)
    inst.llm_calls.add(1, attributes=_LLM_CALL_A)
    inst.llm_tokens.add(1, attributes=_LLM_TOK_A)
    inst.llm_duration.record(1.0, attributes=_LLM_DUR_A)
    inst.event_dispatched.add(1, attributes=_EVENT_A)
    inst.persona_tick_interval.record(1.0, attributes=_TICK_A)
    # RFC 0024 PR 3b — the four ``agent.wake.*`` counters land here as
    # the formal home (PR 1 referenced them in logs but did not register
    # the OTEL instruments).  PR 4's bored-persona cost-regression CI
    # gate asserts all four read zero over a 60-second window — touching
    # them here means a future rename or unit drift trips the inventory
    # test before it surfaces in the gate.
    inst.wake_inbound.add(1, attributes=_WAKE_INBOUND_A)
    inst.wake_scheduled.add(1, attributes=_WAKE_SCHEDULED_A)
    inst.wake_salience.add(1, attributes=_WAKE_SALIENCE_A)
    inst.wake_dropped.add(1, attributes=_WAKE_DROPPED_A)
    # PR-170 M2: ``agent.active`` is exercised with a real ``+1 / -1``
    # round-trip in :class:`TestAgentActiveLifecycle` below, not a no-op
    # ``add(0)`` here.  The previous touch-with-zero existed solely to
    # satisfy the inventory test and masked the fact that no production
    # code path was incrementing the gauge.
    inst.agent_active.add(1, attributes={"agent.id": "t"})
    inst.agent_active.add(-1, attributes={"agent.id": "t"})
    inst.spans_dropped.add(1, attributes=_DROP_A)
    inst.logs_dropped.add(1, attributes=_DROP_A)
    # RFC 0031 Phase 1 — exercise the per-session write counter so the
    # inventory + unit-coverage tests below catch a rename / unit drift.
    inst.sessions_writes.add(
        1,
        attributes={
            "session_id": "legacy",
            "agent.id": "t",
            "surface": "episode",
        },
    )
    # RFC 0026 PR 1 — exercise the declarative-facts counters.  PR 1
    # ships the storage primitive; ``facts.stored`` increments per
    # ``FactStore.store`` call and ``facts.superseded`` increments per
    # latest-asserted-wins supersede write.  ``facts.extraction_failed``
    # is reserved by PR 1 and incremented by PR 2's combined summarize +
    # extract prompt when fact-tuple parsing fails (summary still
    # commits — see RFC 0026 Phase 1 step 4 atomicity contract).
    inst.facts_stored.add(1, attributes={"agent.id": "t"})
    inst.facts_superseded.add(1, attributes={"agent.id": "t"})
    inst.facts_extraction_failed.add(1, attributes={"agent.id": "t"})
    inst.facts_envelope_parse_failed.add(
        1, attributes={"agent.id": "t", "reason": "truncated"},
    )
    # RFC 0030 Tier B (v0.3.8) — the salience-bid skip counter, registered by
    # the ``_metrics_salience`` split. Touched here so a rename or unit drift,
    # or a future drop of ``_mtb`` from the ``register`` loop, trips the
    # inventory/unit tests below rather than surfacing as an AttributeError at
    # the first call site (the counter is a class annotation registered out of
    # line, so mypy cannot catch the omission).
    inst.channel_messages_salience_skipped.add(
        1, attributes={"reason": "channel_too_large"},
    )


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
            # RFC 0031 PR 4 follow-up F1: the per-session write counter
            # drops the ``agent.`` prefix so a single PromQL query like
            # ``sum(rate(sessions_writes_total[5m])) by (session_id)`` sees
            # both the orchestrator (Go ``sessions.writes`` —
            # ``internal/observability/metrics/channel_instruments.go:53``)
            # and the persona-runtime/sub-agent ticks in the same series.
            # All other Python instruments keep the ``agent.`` prefix
            # because they are per-agent metrics; ``sessions.writes`` is a
            # cross-binary RFC 0031 contract, not a per-agent metric.
            "sessions.writes",
            # RFC 0026 PR 1 — declarative-facts tier counters.  All three
            # ship in PR 1 even though ``facts.extraction_failed`` is only
            # incremented by PR 2's extractor; reserving the instrument
            # here means PR 2 is a one-line increment, not a metrics-API
            # change.  ``agent.`` prefix retained — these are per-agent
            # metrics (cardinality bounded by ``agent.id``), unlike the
            # ``sessions.writes`` cross-binary contract.
            "agent.facts.stored",
            "agent.facts.superseded",
            "agent.facts.extraction_failed",
            "agent.facts.envelope_parse_failed",
            # RFC 0024 PR 3b — ``agent.wake.*`` counter family.  All
            # four ship in PR 3b even though PR 1 wires
            # ``agent.wake.dropped`` (substrate-level) and PR 4's
            # channel-dispatch path adds ``agent.wake.inbound`` /
            # ``scheduled`` at meaningful production volume.  Registering
            # all four here means PR 4's cost-regression CI gate is a
            # zero-counter assertion, not a metrics-API change.
            "agent.wake.inbound",
            "agent.wake.scheduled",
            "agent.wake.salience",
            "agent.wake.dropped",
            # RFC 0030 Tier B (v0.3.8) — the salience-bid skip counter. Pinned
            # here so the ``_metrics_salience`` split stays wired into the
            # ``_Instruments`` registration loop.
            "channel.messages.salience_skipped",
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
            "sessions.writes": "{write}",
            "agent.facts.stored": "{fact}",
            "agent.facts.superseded": "{fact}",
            "agent.facts.extraction_failed": "{failure}",
            "agent.facts.envelope_parse_failed": "{failure}",
            "agent.wake.inbound": "{wake}",
            "agent.wake.scheduled": "{wake}",
            "agent.wake.salience": "{wake}",
            "agent.wake.dropped": "{wake}",
            "channel.messages.salience_skipped": "{message}",
        }
        for name, unit in expected_units.items():
            assert seen.get(name) == unit, f"{name} unit={seen.get(name)!r} expected={unit!r}"

    def test_raw_id_usage_counter_is_retired(
        self, metric_reader: InMemoryMetricReader,
    ) -> None:
        """RFC 0033 Phase 3 (deliverable 2) — the ``alias_raw_id_usage``
        gate counter is removed.

        It was the Phase 3 *entrance* signal (raw-ID usage reading zero).
        Phase 3 has now landed — the raw-ID pass-through (#481) and the
        ``_infer_provider`` heuristic are both gone, ``resolve`` rejects
        any non-alias reference — so the counter has nothing left to count
        and no incrementer. Pin its absence from the instrument set.
        """
        inst = pmetrics.get_instruments()
        assert not hasattr(inst, "alias_raw_id_usage"), (
            "persatrix.llm.alias.raw_id_usage is retired — its Phase 3 gate "
            "purpose is fulfilled (raw IDs are now rejected at resolve)"
        )


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

    The instrument is wired in :func:`agents.server_cli.main` (``+1`` after
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
