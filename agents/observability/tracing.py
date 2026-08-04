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
                                    (default: "0.3.13")

Baggage key naming convention
-----------------------------
W3C Baggage entries set on the OTEL context propagate **in plaintext** across
every gRPC hop and to the OTLP backend in trace metadata.  To make the leak
surface easy to audit, all Persatrix-internal baggage keys MUST be namespaced
with the ``persatrix.`` prefix (e.g. ``persatrix.workflow_id``,
``persatrix.task_id``).  Do NOT place user-controlled data, PII, secrets, or
credentials in baggage — anything written here is observable end-to-end.
A lint check enforcing this prefix is tracked for a follow-up to RFC 0019.
See PR #163 review round 2 (Should Fix #5).
"""

from __future__ import annotations

from opentelemetry import trace
from opentelemetry.baggage.propagation import (
    W3CBaggagePropagator,  # type: ignore[import-untyped]
)
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
from opentelemetry.sdk.trace.sampling import (
    ALWAYS_ON,
    ParentBased,
    TraceIdRatioBased,
)
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from ._env import env_str as _env

_BAGGAGE_PROPAGATOR: TextMapPropagator = W3CBaggagePropagator()

# W3CBaggagePropagator has been part of opentelemetry-api since 1.0.0 and
# ``agents/pyproject.toml`` pins ``opentelemetry-api>=1.28.0,<2``, so this
# import is guaranteed to succeed.  An earlier draft of this module had a
# ``try/except ImportError`` fallback to ``B3Format`` here, but B3 is a
# *trace context* propagator (not a baggage propagator) so that fallback
# would have silently dropped baggage on the floor — see PR #163 review.

# Schema URL for the Persatrix observability schema (cross-language contract).
_SCHEMA_URL = "https://persatrix.dev/schemas/observability/1.0.0"

_DEFAULT_SERVICE_NAME = "persatrix-agent"
_DEFAULT_OTLP_ENDPOINT = "http://localhost:4318"
_DEFAULT_SERVICE_VERSION = "0.3.13"

# BatchSpanProcessor tuning — deterministic across environments.
_BSP_MAX_QUEUE_SIZE = 2048
_BSP_MAX_EXPORT_BATCH_SIZE = 512
_BSP_EXPORT_TIMEOUT_MILLIS = 10_000

# Module-level provider reference so shutdown() can flush it.
_provider: TracerProvider | None = None
# NOTE: a ``_DroppingBSP`` subclass and ``_on_drop`` callback hook lived here
# in an earlier draft of this module to expose BatchSpanProcessor queue-overflow
# events.  They were removed for this PR because the override only called the
# parent ``on_end()`` and never invoked ``_on_drop`` — a misleading no-op until
# the actual ``agent.observability.spans.dropped`` counter is wired.  The
# subclass + hook will be reintroduced together in RFC 0019 PR 3 alongside the
# real metric.  See PR #163 review round 2 (Should Fix #4).


def init_tracing(
    *,
    exporter: SpanExporter | None = None,
) -> trace.Tracer:
    """Initialise the global OTEL tracing provider and return the root tracer.

    .. note::
        OTEL's ``trace.set_tracer_provider`` is documented as a one-way
        operation: subsequent calls log a warning and leave the global
        provider untouched.  This function therefore replaces the
        *module-level* ``_provider`` reference (so ``shutdown()`` flushes the
        most recent provider) and re-registers the global propagator on
        every call, but the **global tracer provider is set only once per
        process**.  Tests that need fresh span state should call
        ``init_tracing(exporter=InMemorySpanExporter())`` and use the
        *returned* tracer directly rather than relying on
        ``trace.get_tracer()`` to pick up the new provider.

    Args:
        exporter: Override the default ``OTLPSpanExporter`` — used by tests to
            inject an ``InMemorySpanExporter`` without touching env vars.
    """
    global _provider

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

    resource = Resource.create(attributes=attributes, schema_url=_SCHEMA_URL)

    # Resolve exporter and build the exporter_opts carrier for OTLP.
    #
    # The OTEL spec defines two env vars:
    #   - OTEL_EXPORTER_OTLP_ENDPOINT          → base URL (e.g. ``http://collector:4318``)
    #   - OTEL_EXPORTER_OTLP_TRACES_ENDPOINT   → full traces URL (path-qualified)
    #
    # The Python OTLP HTTP exporter's ``endpoint`` kwarg is interpreted as the
    # *full* traces URL (no automatic path appending).  To match the Go side
    # (``otlptracehttp.WithEndpointURL`` is given the base URL and the SDK
    # appends ``/v1/traces``) and to remain robust if an operator follows the
    # spec strictly and sets ``OTEL_EXPORTER_OTLP_ENDPOINT`` to the *base* URL
    # *or* the *full* traces URL (a common confusion), we normalise here:
    # only append ``/v1/traces`` when the endpoint does not already end with it.
    # See PR #163 review (Must Fix #1) — without this guard, setting
    # ``OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4318/v1/traces`` would
    # produce a silent ``…/v1/traces/v1/traces`` double-path 404.
    normalised_endpoint = otlp_endpoint.rstrip("/")
    if not normalised_endpoint.endswith("/v1/traces"):
        normalised_endpoint = f"{normalised_endpoint}/v1/traces"
    exporter_opts: dict[str, object] = {"endpoint": normalised_endpoint}
    # NOTE: ``OTLPSpanExporter`` defaults to plain HTTP for ``http://`` URLs;
    # no explicit insecure flag is needed.  ``OTEL_EXPORTER_OTLP_INSECURE`` is
    # honoured by the SDK itself when present in the environment.

    # If an explicit exporter was provided (test mode), use SimpleSpanProcessor
    # so spans are exported synchronously — no async queue to flush.
    if exporter is None:
        real_exporter = OTLPSpanExporter(**exporter_opts)  # type: ignore[arg-type]
        # Build the BatchSpanProcessor with explicit queue and batch caps so
        # behaviour is deterministic across environments.  PR 3 will wrap
        # this in a subclass that surfaces queue-drop events to a metric.
        processor: BatchSpanProcessor | SimpleSpanProcessor = BatchSpanProcessor(
            real_exporter,
            max_queue_size=_BSP_MAX_QUEUE_SIZE,
            max_export_batch_size=_BSP_MAX_EXPORT_BATCH_SIZE,
            export_timeout_millis=_BSP_EXPORT_TIMEOUT_MILLIS,
        )
    else:
        # Test path: synchronous export so no flush needed in assertions.
        processor = SimpleSpanProcessor(exporter)

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
    """Flush pending spans and shut down the tracer provider.

    .. note::
        Although declared ``async`` for symmetry with the agent shutdown path,
        the underlying ``TracerProvider.force_flush()`` and ``shutdown()``
        calls are **synchronous and blocking** — they can block the asyncio
        event loop for up to ``_BSP_EXPORT_TIMEOUT_MILLIS`` (10 s) while the
        ``BatchSpanProcessor`` background thread drains.  This is acceptable
        during graceful shutdown (the loop is being torn down anyway) but
        callers running in a hot path should not invoke this function.
        See PR #163 review (Should Fix #4).
    """
    global _provider
    if _provider is not None:
        # ``force_flush`` returns ``False`` on timeout (spans dropped); we do
        # not log this here because the underlying SDK already emits a warning
        # via the ``opentelemetry`` logger on flush failure.
        _provider.force_flush()
        _provider.shutdown()
        _provider = None
