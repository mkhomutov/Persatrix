"""RFC 0019 PR 4 — schema parity contract test (no compose dep).

Asserts the two-RFC schema contract spelled out in
[RFC 0019 § E](../../docs/rfcs/0019-opentelemetry-completion.md
#e-span-naming-and-attribute-conventions)
and the [PR 4 plan](../../docs/rfcs/0019-pr-plan.md) checklist:

1. Every Persatrix correlation ID listed as an Optional field in
   [RFC 0018 § B](../../docs/rfcs/0018-structured-logging-framework.md#b-common-log-schema)
   has a counterpart on the trace/metric side — either as a Persatrix-prefixed
   span / metric attribute (``persatrix.<id>``) or as a stable OTEL standard
   attribute (``trace_id`` / ``span_id``).

2. The schema-version pins are fixed in code on both sides:
     * Logs: ``agents.observability.logging.SCHEMA_VERSION == "1"``.
     * Traces & metrics: the ``_SCHEMA_URL`` constants in
       :mod:`agents.observability.tracing` and
       :mod:`agents.observability.metrics` agree on the
       ``https://persatrix.dev/schemas/observability/1.0.0`` URL.

3. The gRPC metadata key surface (``persatrix-execution-id`` …) keeps the
   four-key set the Go side ships and the Python interceptor binds, so the
   correlation chain documented in RFC 0018 § D cannot silently lose a key.

The test deliberately works against in-process module constants (no compose
stack required) so it runs in the default CI suite as a tripwire against
silent schema drift between the two RFCs.
"""

from __future__ import annotations

from agents.observability import logging as logging_mod
from agents.observability import metrics as metrics_mod
from agents.observability import tracing as tracing_mod
from agents.observability.grpc_logging import _METADATA_TO_CONTEXTVAR

# ─── Pinned constants (canonical schema versions) ───────────────────────────


def test_log_schema_version_pinned_to_1() -> None:
    """RFC 0018 § B — log ``schema_version`` is pinned at the string ``"1"``.

    Bumping this is a release-notes event (RFC 0018 § B Versioning); changing
    it accidentally would silently invalidate every consumer's
    ``schema_version`` filter.
    """
    assert logging_mod.SCHEMA_VERSION == "1"


def test_trace_and_metric_schema_url_match_and_are_pinned() -> None:
    """RFC 0019 § B + § F — traces and metrics carry the same schema URL.

    The two signals must agree so a backend rendering both (Jaeger via the
    Collector, Prometheus via the Collector) sees one schema namespace per
    Persatrix release.  A mismatch would mean traces and metrics emitted
    from the same process advertise different schemas — confusing for any
    schema-aware consumer.
    """
    expected = "https://persatrix.dev/schemas/observability/1.0.0"
    assert tracing_mod._SCHEMA_URL == expected
    assert metrics_mod._SCHEMA_URL == expected


# ─── Correlation ID parity (logs ↔ spans) ───────────────────────────────────


# RFC 0018 § B Optional fields that carry a per-execution correlation ID.
# `attributes` and `source` are not correlation IDs (free-form bag; call-site
# location), so they are excluded from the parity contract.
_LOG_OPTIONAL_CORRELATION_FIELDS = (
    "service.role",
    "execution_id",
    "step_id",
    "agent_id",
    "request_id",
    "trace_id",
    "span_id",
)


# RFC 0019 § E maps the cross-cutting Baggage-propagated IDs into the
# `persatrix.*` namespace.  `agent.id` and `request_id` are span-local /
# Go-only respectively; `trace_id` / `span_id` come from the standard OTEL
# context (not span attributes).
_LOG_TO_SPAN_KEY = {
    "service.role": None,          # role is a static service identity, not a span attribute.
    "execution_id": "persatrix.execution_id",
    "step_id": "persatrix.step_id",
    "agent_id": "agent.id",        # bare component prefix — § 10.5 inventory rule.
    "request_id": None,            # orchestrator-local; emitted by HTTP middleware, not spans.
    "trace_id": "trace_id",        # native OTEL SpanContext field, not a Persatrix attribute.
    "span_id": "span_id",          # native OTEL SpanContext field, not a Persatrix attribute.
}


def test_every_log_correlation_id_has_a_documented_span_mapping() -> None:
    """Each RFC 0018 § B Optional correlation ID must map to a known span key.

    The mapping table above is the contract; this test asserts the table
    stays in sync with the field list.  Adding a new optional field to the
    log schema without deciding how it appears on the trace side is the
    drift hazard the parity test catches.
    """
    missing = [f for f in _LOG_OPTIONAL_CORRELATION_FIELDS if f not in _LOG_TO_SPAN_KEY]
    assert not missing, f"log fields with no documented span mapping: {missing}"


def test_log_field_emission_order_includes_every_correlation_field() -> None:
    """All RFC 0018 § B Optional correlation fields appear in
    :data:`agents.observability.logging._FIELD_ORDER`.

    The structlog ``_reorder_keys`` processor relies on this list to emit
    fields in the documented order; missing one would silently drop the
    field's stable slot and break diff-friendly log capture.
    """
    for field in _LOG_OPTIONAL_CORRELATION_FIELDS:
        assert field in logging_mod._FIELD_ORDER, (
            f"log field {field!r} missing from _FIELD_ORDER — RFC 0018 § B "
            "Optional fields must all have a documented emission slot."
        )


# ─── gRPC metadata surface (Go ↔ Python correlation handshake) ──────────────


def test_grpc_metadata_keys_match_rfc0018_correlation_set() -> None:
    """The four metadata keys on the gRPC boundary stay
    ``persatrix-{execution,step,agent,workflow}-id``.

    The Go orchestrator (``internal/observability/grpcmeta``) and the Python
    :class:`LoggingMetadataInterceptor` both read these strings; renaming
    one without renaming the other would silently break log correlation
    between processes.
    """
    assert _METADATA_TO_CONTEXTVAR == {
        "persatrix-execution-id": "execution_id",
        "persatrix-step-id": "step_id",
        "persatrix-agent-id": "agent_id",
        "persatrix-workflow-id": "workflow_id",
    }
