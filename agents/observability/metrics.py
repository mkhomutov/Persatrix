"""Persatrix OpenTelemetry metrics initialisation (RFC 0019 PR 3).

Mirrors :mod:`agents.observability.tracing`: ``init_metrics()`` builds a
``MeterProvider`` with the OTLP HTTP exporter pointed at the same collector
as traces, and registers the instrument inventory documented in
`RFC 0019 § F <../../docs/rfcs/0019-opentelemetry-completion.md#f-metrics>`_.

Exemplars: histograms created by this module emit exemplars by default
(OTEL Python SDK behaviour when a span context is active at recording
time).  The unit test in ``tests/test_observability_metrics.py`` locks
this in.

Environment variables (all optional; share defaults with tracing):
    OTEL_SERVICE_NAME                 service.name (default: "persatrix-agent")
    OTEL_EXPORTER_OTLP_ENDPOINT      base URL (default: "http://localhost:4318")
    OTEL_METRIC_EXPORT_INTERVAL_MS   PeriodicExportingMetricReader interval
                                      (default: 60_000)
    OTEL_METRIC_EXPORT_TIMEOUT_MS    per-export timeout (default: 10_000)
    PERSATRIX_AGENT_ID               sets ``service.instance.id``
    PERSATRIX_SERVICE_VERSION        sets ``service.version``
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Literal

from opentelemetry import metrics
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    MetricExporter,
    MetricReader,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.resources import SERVICE_NAME, Resource

if TYPE_CHECKING:
    from opentelemetry.metrics import (
        Counter,
        Histogram,
        Meter,
        UpDownCounter,
    )

# Shared schema URL with tracing — documented in RFC 0019 § D.
_SCHEMA_URL = "https://persatrix.dev/schemas/observability/1.0.0"

_DEFAULT_SERVICE_NAME = "persatrix-agent"
_DEFAULT_OTLP_ENDPOINT = "http://localhost:4318"
_DEFAULT_SERVICE_VERSION = "0.2.3"
_DEFAULT_EXPORT_INTERVAL_MS = 60_000
_DEFAULT_EXPORT_TIMEOUT_MS = 10_000

# Module-level singletons so ``shutdown()`` can flush, and instrumentation
# sites can fetch instruments without re-creating them.
_provider: MeterProvider | None = None
_instruments: _Instruments | None = None


def _env(key: str, default: str) -> str:
    v = os.environ.get(key, "").strip()
    return v if v else default


def _int_env(key: str, default: int) -> int:
    v = os.environ.get(key, "").strip()
    if not v:
        return default
    try:
        parsed = int(v)
        return parsed if parsed > 0 else default
    except ValueError:
        return default


class _Instruments:
    """Registered instrument handles — one per documented metric.

    Held as attributes rather than a dict so mypy / IDEs can validate
    call sites.  Names and units match `RFC 0019 § F`_ exactly; the
    parity test in ``test_observability_metrics.py`` asserts this.
    """

    def __init__(self, meter: Meter) -> None:
        # ─── Counters ────────────────────────────────────────────────
        self.tool_invocations: Counter = meter.create_counter(
            name="agent.tool.invocations",
            unit="{invocation}",
            description="Tool executions observed by the agent runtime.",
        )
        self.llm_calls: Counter = meter.create_counter(
            name="agent.llm.calls",
            unit="{call}",
            description="LLM provider invocations.",
        )
        self.llm_tokens: Counter = meter.create_counter(
            name="agent.llm.tokens",
            unit="{token}",
            description="Input and output tokens billed by the LLM provider.",
        )
        self.event_dispatched: Counter = meter.create_counter(
            name="agent.event.dispatched",
            unit="{event}",
            description="Persona events dispatched to an agent.",
        )
        self.spans_dropped: Counter = meter.create_counter(
            name="agent.observability.spans.dropped",
            unit="{span}",
            description=(
                "Spans dropped by the BatchSpanProcessor (queue full or "
                "export error)."
            ),
        )
        self.logs_dropped: Counter = meter.create_counter(
            name="agent.observability.logs.dropped",
            unit="{record}",
            description=(
                "Log records dropped by the agent shipper (queue full or "
                "export error).  Reserved here; emitted by RFC 0018 PR 5."
            ),
        )

        # ─── Histograms ──────────────────────────────────────────────
        self.tool_duration: Histogram = meter.create_histogram(
            name="agent.tool.duration",
            unit="ms",
            description="Wall-clock duration of tool executions.",
        )
        self.llm_duration: Histogram = meter.create_histogram(
            name="agent.llm.duration",
            unit="ms",
            description="Wall-clock duration of LLM calls.",
        )
        self.persona_tick_interval: Histogram = meter.create_histogram(
            name="agent.persona.tick.interval",
            unit="ms",
            description=(
                "Wall-clock interval between consecutive ``on_tick()`` "
                "invocations per agent."
            ),
        )

        # ─── UpDownCounters (gauges) ────────────────────────────────
        self.agent_active: UpDownCounter = meter.create_up_down_counter(
            name="agent.active",
            unit="{agent}",
            description="Active agent instances hosted by this process.",
        )


def init_metrics(
    *,
    exporter: MetricExporter | None = None,
    reader: MetricReader | None = None,
) -> Meter:
    """Initialise the global OTEL meter provider and return the root meter.

    Args:
        exporter: Override the default OTLP HTTP exporter.  Used by tests to
            inject a mock; ignored when ``reader`` is also supplied.
        reader: Override the ``PeriodicExportingMetricReader`` entirely.
            Tests pass an ``InMemoryMetricReader`` here.

    .. note::
        Like ``trace.set_meter_provider``, ``metrics.set_meter_provider`` is
        documented as a one-way operation per process.  Subsequent calls
        replace the module-level provider reference (so ``shutdown()``
        flushes the most recent provider) but the global SDK state is set
        once.  Tests that need fresh meter state should pass a fresh
        ``InMemoryMetricReader`` and read the *returned* meter directly.
    """
    global _provider, _instruments

    service_name = _env("OTEL_SERVICE_NAME", _DEFAULT_SERVICE_NAME)
    otlp_endpoint = _env("OTEL_EXPORTER_OTLP_ENDPOINT", _DEFAULT_OTLP_ENDPOINT)
    instance_id = _env("PERSATRIX_AGENT_ID", "")
    service_version = _env("PERSATRIX_SERVICE_VERSION", _DEFAULT_SERVICE_VERSION)
    export_interval_ms = _int_env(
        "OTEL_METRIC_EXPORT_INTERVAL_MS", _DEFAULT_EXPORT_INTERVAL_MS,
    )
    export_timeout_ms = _int_env(
        "OTEL_METRIC_EXPORT_TIMEOUT_MS", _DEFAULT_EXPORT_TIMEOUT_MS,
    )

    attributes: dict[str, str] = {
        SERVICE_NAME: service_name,
        "service.version": service_version,
        "service.kind": "agent",
    }
    if instance_id:
        attributes["service.instance.id"] = instance_id

    resource = Resource.create(attributes=attributes, schema_url=_SCHEMA_URL)

    if reader is None:
        if exporter is None:
            # OTLP HTTP exporter — path ``/v1/metrics`` appended by the
            # exporter itself (unlike the traces exporter, which takes the
            # full URL).  Normalise mirror the tracing.py guard so operators
            # who set ``OTEL_EXPORTER_OTLP_ENDPOINT`` to a path-qualified
            # URL get consistent behaviour.
            normalised = otlp_endpoint.rstrip("/")
            if normalised.endswith("/v1/metrics"):
                normalised = normalised[: -len("/v1/metrics")]
            exporter = OTLPMetricExporter(endpoint=f"{normalised}/v1/metrics")
        reader = PeriodicExportingMetricReader(
            exporter,
            export_interval_millis=export_interval_ms,
            export_timeout_millis=export_timeout_ms,
        )

    _provider = MeterProvider(resource=resource, metric_readers=[reader])
    metrics.set_meter_provider(_provider)

    meter = _provider.get_meter("persatrix", schema_url=_SCHEMA_URL)
    _instruments = _Instruments(meter)
    return meter


def get_instruments() -> _Instruments:
    """Return the module-level instrument bag.

    Raises ``RuntimeError`` if ``init_metrics()`` has not been called —
    call sites should treat missing instruments as a programmer error, not
    silently no-op (which would hide a startup ordering bug).
    """
    if _instruments is None:
        raise RuntimeError(
            "metrics not initialised — call init_metrics() before "
            "recording metrics",
        )
    return _instruments


# Non-raising convenience used by hot paths that may be called before
# init_metrics() (e.g. module import-time) — returns ``None`` instead of
# raising so the caller can no-op.
def try_get_instruments() -> _Instruments | None:
    return _instruments


async def shutdown() -> None:
    """Flush pending metric exports and shut down the meter provider.

    Declared ``async`` for symmetry with :func:`tracing.shutdown`; the
    underlying ``force_flush`` / ``shutdown`` calls are synchronous.
    """
    global _provider, _instruments
    if _provider is not None:
        _provider.force_flush()
        _provider.shutdown()
        _provider = None
        _instruments = None


# ─── Attribute helpers ──────────────────────────────────────────────────
#
# Low-cardinality attribute builders used at record sites.  Kept here
# (not in ``spans.py``) so the parity test can assert both span and
# metric sites use the same key names for shared dimensions.

TokenType = Literal["input", "output"]


def tool_attrs(
    *, agent_id: str, tool_name: str, success: bool,
) -> dict[str, str | bool]:
    return {
        "agent.id": agent_id,
        "tool.name": tool_name,
        "tool.success": success,
    }


def llm_call_attrs(
    *,
    agent_id: str,
    system: str,
    request_model: str,
    cache_hit: bool = False,
) -> dict[str, str | bool]:
    return {
        "agent.id": agent_id,
        "gen_ai.system": system,
        "gen_ai.request.model": request_model,
        "persatrix.llm.cache.hit": cache_hit,
    }


def llm_token_attrs(
    *, agent_id: str, request_model: str, token_type: TokenType,
) -> dict[str, str]:
    return {
        "agent.id": agent_id,
        "gen_ai.request.model": request_model,
        "gen_ai.token.type": token_type,
    }


def llm_duration_attrs(
    *, agent_id: str, request_model: str,
) -> dict[str, str]:
    return {"agent.id": agent_id, "gen_ai.request.model": request_model}


def event_attrs(*, agent_id: str, event_type: str) -> dict[str, str]:
    return {"agent.id": agent_id, "event.type": event_type}


def tick_attrs(*, agent_id: str) -> dict[str, str]:
    return {"agent.id": agent_id}


def drop_attrs(*, agent_id: str, reason: str) -> dict[str, str]:
    return {"agent.id": agent_id, "reason": reason}
