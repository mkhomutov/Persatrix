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

import asyncio
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

from ._env import env_int as _int_env
from ._env import env_str as _env

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
_DEFAULT_SERVICE_VERSION = "0.3.10"
_DEFAULT_EXPORT_INTERVAL_MS = 60_000
_DEFAULT_EXPORT_TIMEOUT_MS = 10_000

# Module-level singletons so ``shutdown()`` can flush, and instrumentation
# sites can fetch instruments without re-creating them.
_provider: MeterProvider | None = None
_instruments: _Instruments | None = None

# PR-170 S5: cache ``PERSATRIX_AGENT_ID`` once via ``current_agent_id()`` so
# hot paths skip ``os.environ`` lookups; the cache is populated lazily on
# first read (test-fixture friendly) and not refreshed thereafter (agent
# identity is set at process start).
_AGENT_ID: str | None = None


def current_agent_id() -> str:
    """Return this process's agent id, defaulting to ``"unknown"``.

    Cached after first call.  Recording sites should prefer this over
    ``os.environ.get("PERSATRIX_AGENT_ID", "unknown")`` so the env-var
    lookup is not in every tool / LLM hot path.
    """
    global _AGENT_ID
    if _AGENT_ID is None:
        _AGENT_ID = os.environ.get("PERSATRIX_AGENT_ID", "").strip() or "unknown"
    return _AGENT_ID


def set_current_agent_id(agent_id: str) -> None:
    """Bind this process's agent id for observability (F-5 — agents launch
    with ``--agent``, not ``PERSATRIX_AGENT_ID``). Called once at startup;
    overrides the lazy cache; blank → ``"unknown"`` like the env fallback.
    """
    global _AGENT_ID
    _AGENT_ID = agent_id.strip() or "unknown"


class _Instruments:
    """Registered instrument handles — one per documented metric.

    Held as attributes rather than a dict so mypy / IDEs can validate
    call sites.  Names and units match `RFC 0019 § F`_ exactly; the
    parity test in ``test_observability_metrics.py`` asserts this.
    """

    # Interaction-lifecycle + facts-tier counters are registered by the
    # ``_metrics_interactions`` / ``_metrics_facts`` modules; annotations keep mypy happy.
    interactions_opened: Counter
    interactions_closed: Counter
    interactions_closed_by_idle_gap: Counter
    interactions_closed_by_structural: Counter
    interactions_closed_by_max_turns: Counter
    interactions_closed_by_topic_shift: Counter
    interactions_closed_by_shutdown: Counter
    interactions_closed_by_cost: Counter
    interactions_summary_failed: Counter
    interactions_summary_unleased: Counter
    interactions_janitor_failed: Counter
    facts_stored: Counter
    facts_superseded: Counter
    facts_extraction_failed: Counter
    facts_envelope_parse_failed: Counter
    facts_injected: Counter
    persona_tick_idle: Counter
    # RFC 0024 PR 3b — registered by :mod:`._metrics_wakes`.
    wake_inbound: Counter
    wake_scheduled: Counter
    wake_salience: Counter
    wake_dropped: Counter
    # RFC 0021 Phase 1 — registered by :mod:`._metrics_temporal`.
    temporal_now_anchor_emitted: Counter
    temporal_recency_rendered: Counter
    channel_messages_salience_skipped: Counter  # RFC 0030 Tier B — _metrics_salience
    deliberation_parse_failures: Counter  # RFC 0051 Phase 1a — _metrics_salience

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
        # RFC 0033 Phase 3: the ``persatrix.llm.alias.raw_id_usage`` gate
        # counter is retired — with the raw-ID pass-through and ``_infer_provider``
        # both removed and ``resolve`` rejecting any non-alias reference, there
        # is nothing left to count.
        self.event_dispatched: Counter = meter.create_counter(
            name="agent.event.dispatched",
            unit="{event}",
            description="Persona events dispatched to an agent.",
        )
        # RFC 0011 PR 4b: response-gate suppression counter. Increments
        # once per CHANNEL_MESSAGE dispatch the gate suppresses. Carries
        # a ``policy`` attribute (``when_mentioned``/``always``) so
        # operators can spot a stuck-on-mute agent (huge ``always``
        # gate counts → wrong policy on the membership) or surface the
        # natural baseline (``when_mentioned`` events that did not
        # mention the recipient). ``never`` policies are filtered
        # upstream of dispatch and never reach the gate.
        self.channel_messages_gated: Counter = meter.create_counter(
            name="channel.messages.gated",
            unit="{message}",
            description=(
                "Channel messages suppressed before the quality LLM turn. "
                "Attrs: channel_id, policy (RFC 0011 §D); RFC 0030 Tier B "
                "fires policy=low_salience after the cheap bid, +a reason attr."
            ),
        )
        # RFC 0011 PR 5 follow-up — separate from ``channel.messages.gated``
        # so a startup catch-up burst does not mask a gate-suppression spike.
        self.channel_messages_replayed: Counter = meter.create_counter(
            name="channel.messages.replayed",
            unit="{message}",
            description=(
                "Channel messages replayed through the on-startup "
                "catch-up fetch (RFC 0011 OQ #8). Attribute: channel_id."
            ),
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

        from . import _metrics_conversation_window as _mcw
        from . import _metrics_facts as _mf
        from . import _metrics_interactions as _mi
        from . import _metrics_persona_tick as _mp
        from . import _metrics_salience as _msal
        from . import _metrics_temporal as _mt
        from . import _metrics_wakes as _mw
        for mod in (_mi, _mf, _mp, _mw, _mt, _msal, _mcw):  # registered under the file-size cap
            mod.register(self, meter)

        # ─── Shared memory pools (RFC 0008 PR plan PR 4) ─────────────
        # Recorded by ``agents.memory.shared_pool`` ACL gates.  ``denied``
        # carries an ``operation`` attribute (read|write|publish) so
        # operators can distinguish trust-boundary breaches from misrouted
        # writers without joining traces.
        self.shared_pool_reads: Counter = meter.create_counter(
            name="agent.shared_pool.reads",
            unit="{call}",
            description=(
                "Reads served by a SharedMemoryPool (post-ACL).  Attributes: "
                "pool, agent.id, result.count."
            ),
        )
        self.shared_pool_writes: Counter = meter.create_counter(
            name="agent.shared_pool.writes",
            unit="{entry}",
            description=(
                "Entries written to a SharedMemoryPool (post-ACL + provenance "
                "injection).  Attributes: pool, agent.id."
            ),
        )
        self.shared_pool_denied: Counter = meter.create_counter(
            name="agent.shared_pool.denied",
            unit="{call}",
            description=(
                "ACL or sensitivity denials raised by SharedMemoryPool.  "
                "Attributes: pool, agent.id, operation."
            ),
        )
        self.shared_pool_evictions: Counter = meter.create_counter(
            name="agent.shared_pool.evictions",
            unit="{entry}",
            description=(
                "Entries evicted by the FIFO ``max_entries`` cap on a "
                "SharedMemoryPool.  Attributes: pool."
            ),
        )

        # Python mirror of channel_instruments.go:53 ``sessions.writes``
        # (RFC 0031 Phase 1); one tick per store_episode / record_interaction.
        # Name omits ``agent.`` prefix — cross-binary contract (PR 4 F1).
        self.sessions_writes: Counter = meter.create_counter(
            name="sessions.writes",
            unit="{write}",
            description=(
                "Memory-tier writes attributed to a session_id "
                "(RFC 0031 Phase 1). Attrs: session_id, agent.id, surface."
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

    Declared ``async`` for symmetry with :func:`tracing.shutdown`.  The
    underlying ``force_flush`` / ``shutdown`` calls on
    ``PeriodicExportingMetricReader`` are synchronous **and can block for up
    to ``OTEL_METRIC_EXPORT_TIMEOUT_MS``** (default 10 s) when the collector
    is slow or unreachable (PR-170 S3).  Offload to a worker thread via
    :func:`asyncio.to_thread` so teardown does not stall the event loop —
    the server's shutdown path awaits other async cleanup (gRPC stop,
    tracing flush) concurrently with this, and blocking here would serialise
    them.
    """
    global _provider, _instruments
    if _provider is not None:
        provider = _provider
        await asyncio.to_thread(provider.force_flush)
        await asyncio.to_thread(provider.shutdown)
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
    *,
    agent_id: str,
    request_model: str,
    success: bool = True,
    error_type: str | None = None,
) -> dict[str, str | bool]:
    """Duration-histogram dimensions for the ``agent.llm.duration`` metric.

    ``success`` partitions the histogram so failure-path latencies (often
    dominated by provider timeouts / retry jitter) do not pollute success
    percentiles (PR-170 S1).  Mirrors the shape of :func:`tool_attrs`.
    ``error_type`` is a low-cardinality classifier — ``rate_limit``,
    ``timeout``, ``provider_error`` — set only when ``success`` is ``False``.
    """
    attrs: dict[str, str | bool] = {
        "agent.id": agent_id,
        "gen_ai.request.model": request_model,
        "llm.success": success,
    }
    if error_type is not None:
        attrs["error.type"] = error_type
    return attrs


def event_attrs(*, agent_id: str, event_type: str) -> dict[str, str]:
    return {"agent.id": agent_id, "event.type": event_type}


def gate_attrs(*, channel_id: str, policy: str) -> dict[str, str]:
    """Attribute set for the ``channel.messages.gated`` counter (RFC 0011 PR 4b).

    Labels match the RFC 0011 §D specification exactly: ``channel_id``
    and ``policy``. ``subscriber_id`` (the agent id) is deliberately
    **not** a label — cardinality scales as members × channels × policies
    (~30,000 series at N=200) and the agent identity is already carried
    by the OTLP resource attribute ``service.instance.id`` (set from
    ``PERSATRIX_AGENT_ID`` at ``init_metrics`` time). Per-subscriber
    drill-down lives in the publish/delivery spans (RFC 0011 §G), not on
    the gate counter.
    """
    return {"channel_id": channel_id, "policy": policy}


def replay_attrs(*, channel_id: str) -> dict[str, str]:
    """Attribute set for ``channel.messages.replayed`` (RFC 0011 PR 5
    follow-up). See :func:`gate_attrs` for the cardinality rationale."""
    return {"channel_id": channel_id}


def tick_attrs(*, agent_id: str) -> dict[str, str]:
    return {"agent.id": agent_id}


def drop_attrs(*, agent_id: str, reason: str) -> dict[str, str]:
    return {"agent.id": agent_id, "reason": reason}
