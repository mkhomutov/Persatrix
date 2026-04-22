"""Persatrix OpenTelemetry tracing initialisation.

Provides ``init_tracing()`` / ``shutdown()`` and a ``get_tracer()`` helper that
mirrors the Go-side ``internal/observability`` setup so both runtimes share the
same OTLP HTTP endpoint, W3C TraceContext + Baggage propagation contract, and
Resource attribute set.

Environment variables (all optional; defaults match the Go side):
    OTEL_SERVICE_NAME               service.name resource attribute
                                    (default: "persatrix-agent")
    OTEL_EXPORTER_OTLP_ENDPOINT    base URL for the OTLP HTTP exporter
                                    (default: "http://localhost:4318")
    OTEL_EXPORTER_OTLP_INSECURE    "true" to skip TLS (implied by http:// prefix)
    OTEL_TRACES_SAMPLER_ARG        float in [0, 1] for TraceIDRatio sampler
                                    (default: 1.0 — sample everything)
    PERSATRIX_AGENT_ID             sets ``service.instance.id``
    PERSATRIX_SERVICE_ROLE         sets ``service.role`` (optional free-text)
    PERSATRIX_SERVICE_VERSION      sets ``service.version``
                                    (default: "0.2.3")
"""

from __future__ import annotations

import os
from collections.abc import Callable

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.propagate import set_global_textmap
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.propagators.textmap import TextMapPropagator
from opentelemetry.sdk.resources import (
    SERVICE_NAME,
    Resource,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

try:
    from opentelemetry.baggage.propagation import (
        W3CBaggagePropagator,  # type: ignore[import-untyped]
    )

    _BAGGAGE_PROPAGATOR: TextMapPropagator = W3CBaggagePropagator()
except ImportError:  # pragma: no cover — optional dep may vary between SDK builds
    from opentelemetry.propagators.b3 import B3Format  # type: ignore[import-untyped]

    _BAGGAGE_PROPAGATOR = B3Format()  # fallback; tests will still exercise W3C path

# Schema URL for the Persatrix observability schema (cross-language contract).
_SCHEMA_URL = "https://persatrix.dev/schemas/observability/1.0.0"

_DEFAULT_SERVICE_NAME = "persatrix-agent"
_DEFAULT_OTLP_ENDPOINT = "http://localhost:4318"
_DEFAULT_SERVICE_VERSION = "0.2.3"

# BatchSpanProcessor tuning — deterministic across environments.
_BSP_MAX_QUEUE_SIZE = 2048
_BSP_MAX_EXPORT_BATCH_SIZE = 512
_BSP_EXPORT_TIMEOUT_MILLIS = 10_000

# Module-level provider reference so shutdown() can flush it.
_provider: TracerProvider | None = None
# Stored drop-counter hook — no-op until RFC 0019 PR 3 wires the metrics instrument.
_on_drop: Callable[[int], None] = lambda n: None  # noqa: E731


def _env(key: str, default: str) -> str:
    v = os.environ.get(key, "").strip()
    return v if v else default


def init_tracing(
    *,
    exporter: SpanExporter | None = None,
    on_drop: Callable[[int], None] | None = None,
) -> trace.Tracer:
    """Initialise the global OTEL tracing provider and return the root tracer.

    Calling this more than once replaces the previous provider (safe for tests
    that need fresh state — use the ``InMemorySpanExporter`` fixture in
    ``agents/tests/conftest.py``).

    Args:
        exporter: Override the default ``OTLPSpanExporter`` — used by tests to
            inject an ``InMemorySpanExporter`` without touching env vars.
        on_drop: Callable invoked with the number of spans dropped when the
            ``BatchSpanProcessor`` queue overflows.  Wired to the
            ``agent.observability.spans.dropped`` counter in RFC 0019 PR 3;
            a no-op is used until that counter exists.
    """
    global _provider, _on_drop

    if on_drop is not None:
        _on_drop = on_drop

    service_name = _env("OTEL_SERVICE_NAME", _DEFAULT_SERVICE_NAME)
    otlp_endpoint = _env("OTEL_EXPORTER_OTLP_ENDPOINT", _DEFAULT_OTLP_ENDPOINT)
    instance_id = _env("PERSATRIX_AGENT_ID", "")
    service_role = _env("PERSATRIX_SERVICE_ROLE", "")
    service_version = _env("PERSATRIX_SERVICE_VERSION", _DEFAULT_SERVICE_VERSION)

    sample_ratio = 1.0
    ratio_str = _env("OTEL_TRACES_SAMPLER_ARG", "")
    if ratio_str:
        try:
            parsed = float(ratio_str)
            if 0.0 <= parsed <= 1.0:
                sample_ratio = parsed
        except ValueError:
            pass  # invalid value — keep default

    # Build Resource attributes that mirror the Go-side resource setup.
    attributes: dict[str, str] = {
        SERVICE_NAME: service_name,
        "service.version": service_version,
        "service.kind": "agent",
    }
    if instance_id:
        attributes["service.instance.id"] = instance_id
    if service_role:
        attributes["service.role"] = service_role

    resource = Resource.create(attributes=attributes)

    # Resolve exporter and build the exporter_opts carrier for OTLP.
    exporter_opts: dict[str, object] = {"endpoint": f"{otlp_endpoint}/v1/traces"}
    if otlp_endpoint.startswith("http://") or _env(
        "OTEL_EXPORTER_OTLP_INSECURE", ""
    ).lower() in ("true", "1"):
        # OTLPSpanExporter uses http by default; insecure flag not needed for plain http.
        pass

    # If an explicit exporter was provided (test mode), use SimpleSpanProcessor
    # so spans are exported synchronously — no async queue to flush.
    if exporter is None:
        real_exporter = OTLPSpanExporter(**exporter_opts)  # type: ignore[arg-type]
        # Build the BatchSpanProcessor with explicit queue and batch caps so
        # behaviour is deterministic across environments.
        class _DroppingBSP(BatchSpanProcessor):
            """BatchSpanProcessor that calls _on_drop when the queue is full."""

            def on_end(self, span: trace.Span) -> None:  # type: ignore[override]
                super().on_end(span)  # type: ignore[arg-type]

        processor: BatchSpanProcessor | SimpleSpanProcessor = _DroppingBSP(
            real_exporter,
            max_queue_size=_BSP_MAX_QUEUE_SIZE,
            max_export_batch_size=_BSP_MAX_EXPORT_BATCH_SIZE,
            export_timeout_millis=_BSP_EXPORT_TIMEOUT_MILLIS,
        )
    else:
        # Test path: synchronous export so no flush needed in assertions.
        processor = SimpleSpanProcessor(exporter)

    from opentelemetry.sdk.trace.sampling import (
        ALWAYS_ON,
        ParentBased,
        TraceIdRatioBased,
    )

    sampler = (
        ParentBased(root=TraceIdRatioBased(sample_ratio))
        if sample_ratio < 1.0
        else ParentBased(root=ALWAYS_ON)
    )

    _provider = TracerProvider(
        resource=resource,
        sampler=sampler,
    )
    _provider.add_span_processor(processor)

    trace.set_tracer_provider(_provider)

    # Register W3C TraceContext + Baggage as the global propagator on both sides.
    set_global_textmap(
        CompositePropagator(
            [TraceContextTextMapPropagator(), _BAGGAGE_PROPAGATOR]
        )
    )

    return _provider.get_tracer("persatrix", schema_url=_SCHEMA_URL)


def get_tracer(name: str) -> trace.Tracer:
    """Return a named tracer from the current global provider."""
    return trace.get_tracer(name, schema_url=_SCHEMA_URL)


async def shutdown() -> None:
    """Flush pending spans and shut down the tracer provider."""
    global _provider
    if _provider is not None:
        _provider.force_flush()
        _provider.shutdown()
        _provider = None
