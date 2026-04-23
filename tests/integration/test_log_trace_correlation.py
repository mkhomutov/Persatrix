"""RFC 0019 PR 4 — joint log↔trace correlation contract test (no compose dep).

Companion to ``tests/integration/test_logs_correlation.py`` (which exercises
the cross-process metadata path via an in-process gRPC server).  This test
verifies the **single-process** invariant that an active OTEL span context
is enriched onto every log record emitted under it:

    * ``trace_id`` — present and matches the active span's hex trace id.
    * ``span_id`` — present and matches the active span's hex span id.
    * Both fields are **omitted** (not emitted as empty strings) when no
      span is in scope, per the RFC 0018 § B Optional-fields contract.
    * Known Baggage entries (the ``persatrix.*`` namespace from RFC 0019 § E)
      are readable via ``baggage.get_baggage()`` from inside the span scope.

Runs in the default unit-test path — no compose stack required.  The
compose-gated end-to-end test (`test_observability_e2e.py`) covers the
multi-process variant against a real Collector + Jaeger + Loki + Prometheus
stack.
"""

from __future__ import annotations

import io
import json
from collections.abc import Iterator

import pytest
import structlog
from opentelemetry import baggage, context, trace
from opentelemetry.sdk.trace import TracerProvider

from agents.observability import logging as logging_mod
from agents.observability.logging import (
    SCHEMA_VERSION,
    configure_logging,
    get_logger,
)
from agents.observability.redact import NoopRedactor

# ─── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_logging_state() -> Iterator[None]:
    """Drop any prior structlog configuration so each test sees a fresh chain."""
    structlog.contextvars.clear_contextvars()
    logging_mod._configured = False
    logging_mod._redactor = NoopRedactor()
    structlog.reset_defaults()
    yield
    structlog.contextvars.clear_contextvars()
    logging_mod._configured = False
    logging_mod._redactor = NoopRedactor()
    structlog.reset_defaults()


@pytest.fixture
def captured_stderr(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    """Capture stderr so we can read each emitted log line back as JSON."""
    import sys as _real_sys

    buf = io.StringIO()

    class _SysShim:
        stderr = buf

        def __getattr__(self, name: str) -> object:
            return getattr(_real_sys, name)

    monkeypatch.setattr("sys.stderr", buf)
    monkeypatch.setattr(logging_mod, "sys", _SysShim())
    return buf


@pytest.fixture
def tracer() -> trace.Tracer:
    """Install a vanilla SDK ``TracerProvider`` if one is not already global.

    OTEL's ``set_tracer_provider`` is one-way per process; if an earlier test
    in the same pytest session set a provider, we reuse it (no SimpleSpan
    processor needed — the test inspects the span context directly, not
    exported spans).
    """
    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        trace.set_tracer_provider(TracerProvider())
    return trace.get_tracer("test.log_trace_correlation")


# ─── Tests ───────────────────────────────────────────────────────────────────


def _emitted_records(buf: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]


def test_trace_and_span_id_present_on_log_inside_span(
    captured_stderr: io.StringIO, tracer: trace.Tracer
) -> None:
    """Inside a span, every log record carries ``trace_id`` + ``span_id``."""
    configure_logging(service_kind="agent", service_instance="test-agent")
    logger = get_logger("test.correlation")

    with tracer.start_as_current_span("unit-correlation") as span:
        ctx = span.get_span_context()
        expected_trace = format(ctx.trace_id, "032x")
        expected_span = format(ctx.span_id, "016x")
        logger.info("inside span")

    records = _emitted_records(captured_stderr)
    assert len(records) == 1
    rec = records[0]
    assert rec["schema_version"] == SCHEMA_VERSION
    assert rec["trace_id"] == expected_trace
    assert rec["span_id"] == expected_span


def test_trace_and_span_id_omitted_when_no_span_active(
    captured_stderr: io.StringIO,
) -> None:
    """Outside a span, ``trace_id`` / ``span_id`` are omitted entirely.

    RFC 0018 § B specifies the Optional fields are *omitted, not empty* when
    no OTEL context is active.  This guards against a regression where the
    enricher accidentally emits empty-string values that would show up as
    log noise and confuse trace-correlation queries.
    """
    configure_logging(service_kind="agent", service_instance="test-agent")
    logger = get_logger("test.correlation")
    logger.info("outside span")

    records = _emitted_records(captured_stderr)
    assert len(records) == 1
    rec = records[0]
    assert "trace_id" not in rec
    assert "span_id" not in rec


def test_baggage_entries_readable_inside_span(tracer: trace.Tracer) -> None:
    """RFC 0019 § E baggage keys round-trip via ``baggage.get_baggage()``.

    This is the contract the cross-process correlation test
    (``tests/integration/test_logs_correlation.py``) relies on: enrichers on
    both sides read the same baggage namespace, so a log line emitted inside
    a span scope can pull workflow-context attributes without manual
    propagation.
    """
    ctx = baggage.set_baggage("persatrix.workflow_id", "wf-parity-1")
    ctx = baggage.set_baggage("persatrix.execution_id", "exec-parity-1", context=ctx)
    token = context.attach(ctx)
    try:
        with tracer.start_as_current_span("baggage-readable"):
            assert baggage.get_baggage("persatrix.workflow_id") == "wf-parity-1"
            assert baggage.get_baggage("persatrix.execution_id") == "exec-parity-1"
    finally:
        context.detach(token)
