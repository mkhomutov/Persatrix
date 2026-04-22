"""Unit tests for RFC 0019 Phase 1 — agents/observability/tracing.py.

Asserts:
  1. ``init_tracing()`` returns a working tracer.
  2. The TracerProvider resource carries the documented attributes and
     ``schema_url`` (Persatrix observability schema).
  3. ``shutdown()`` flushes pending spans and sets the module-level
     provider to None.
  4. Missing env vars fall back to the documented defaults.
  5. ``get_tracer(name)`` returns a tracer backed by the current provider.
"""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from agents.observability import tracing as _tracing_module
from agents.observability.tracing import (
    _DEFAULT_SERVICE_NAME,
    _SCHEMA_URL,
    get_tracer,
    init_tracing,
    shutdown,
)

# ─── helpers ─────────────────────────────────────────────────────────────────

def _exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


# ─── init_tracing ─────────────────────────────────────────────────────────────


class TestInitTracing:
    def test_returns_tracer(self) -> None:
        """init_tracing() returns a usable Tracer."""

        exporter = _exporter()
        tracer = init_tracing(exporter=exporter)
        assert tracer is not None

        with tracer.start_as_current_span("test.span") as span:
            assert span is not None

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].name == "test.span"

    def test_resource_has_service_name(self) -> None:
        """Resource contains service.name (defaults to persatrix-agent)."""
        exporter = _exporter()
        init_tracing(exporter=exporter)

        assert _tracing_module._provider is not None
        attrs = _tracing_module._provider.resource.attributes
        assert attrs.get("service.name") == _DEFAULT_SERVICE_NAME

    def test_resource_service_name_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """OTEL_SERVICE_NAME env var overrides the default service name."""
        monkeypatch.setenv("OTEL_SERVICE_NAME", "custom-agent")
        exporter = _exporter()
        init_tracing(exporter=exporter)

        assert _tracing_module._provider is not None
        assert _tracing_module._provider.resource.attributes.get("service.name") == "custom-agent"

    def test_resource_has_schema_url(self) -> None:
        """The tracer's schema_url matches the Persatrix observability contract."""

        exporter = _exporter()
        tracer = init_tracing(exporter=exporter)

        # The schema_url is embedded in the tracer's instrumentation scope.
        with tracer.start_as_current_span("probe") as span:
            scope = span.instrumentation_scope  # type: ignore[union-attr,attr-defined]
            assert scope is not None
            assert scope.schema_url == _SCHEMA_URL

    def test_resource_has_service_kind_agent(self) -> None:
        """Resource carries service.kind = 'agent'."""
        exporter = _exporter()
        init_tracing(exporter=exporter)

        assert _tracing_module._provider is not None
        assert _tracing_module._provider.resource.attributes.get("service.kind") == "agent"

    def test_resource_service_instance_id_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PERSATRIX_AGENT_ID populates service.instance.id."""
        monkeypatch.setenv("PERSATRIX_AGENT_ID", "ember-owl")
        exporter = _exporter()
        init_tracing(exporter=exporter)

        assert _tracing_module._provider is not None
        assert (
            _tracing_module._provider.resource.attributes.get("service.instance.id")
            == "ember-owl"
        )

    def test_resource_instance_id_omitted_when_no_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """service.instance.id is absent when PERSATRIX_AGENT_ID is unset."""
        monkeypatch.delenv("PERSATRIX_AGENT_ID", raising=False)
        exporter = _exporter()
        init_tracing(exporter=exporter)

        assert _tracing_module._provider is not None
        assert "service.instance.id" not in _tracing_module._provider.resource.attributes

    def test_missing_env_vars_use_defaults(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Missing OTEL_SERVICE_NAME falls back to 'persatrix-agent'."""
        monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
        exporter = _exporter()
        init_tracing(exporter=exporter)

        assert _tracing_module._provider is not None
        assert (
            _tracing_module._provider.resource.attributes.get("service.name")
            == _DEFAULT_SERVICE_NAME
        )

    def test_invalid_sample_ratio_env_defaults_to_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Invalid OTEL_TRACES_SAMPLER_ARG silently defaults to 1.0."""
        monkeypatch.setenv("OTEL_TRACES_SAMPLER_ARG", "not-a-float")
        exporter = _exporter()
        tracer = init_tracing(exporter=exporter)
        # Should not raise; a span started here should be exported.
        with tracer.start_as_current_span("sampler.probe"):
            pass
        # Use _provider's tracer directly to avoid global-override issue.
        assert len(exporter.get_finished_spans()) >= 1


# ─── shutdown ────────────────────────────────────────────────────────────────


class TestShutdown:
    async def test_shutdown_flushes_spans(self) -> None:
        """shutdown() force-flushes pending spans before returning."""

        exporter = _exporter()
        tracer = init_tracing(exporter=exporter)

        with tracer.start_as_current_span("flush.me"):
            pass

        # Before shutdown, the span may still be in-queue (BatchSpanProcessor).
        # shutdown() must flush it.
        await shutdown()

        spans = exporter.get_finished_spans()
        assert len(spans) >= 1
        assert any(s.name == "flush.me" for s in spans)

    async def test_shutdown_clears_module_provider(self) -> None:
        """After shutdown(), the module-level _provider is None."""
        from agents.observability import tracing as _tracing

        exporter = _exporter()
        init_tracing(exporter=exporter)
        assert _tracing._provider is not None

        await shutdown()

        assert _tracing._provider is None

    async def test_double_shutdown_is_safe(self) -> None:
        """Calling shutdown() twice does not raise."""
        exporter = _exporter()
        init_tracing(exporter=exporter)
        await shutdown()
        await shutdown()  # must not raise


# ─── get_tracer ──────────────────────────────────────────────────────────────


class TestGetTracer:
    def test_returns_tracer_with_schema_url(self) -> None:
        """get_tracer(name) returns a tracer carrying the schema_url."""
        exporter = _exporter()
        init_tracing(exporter=exporter)

        tracer = get_tracer("my.module")
        with tracer.start_as_current_span("gt.probe") as span:
            scope = span.instrumentation_scope  # type: ignore[union-attr,attr-defined]
            assert scope is not None
            assert scope.schema_url == _SCHEMA_URL
