"""Unit tests for RFC 0018 PR 1 — agents/observability/logging.py.

Asserts:
  1. JSON output shape contains the full required-field set in documented order.
  2. ``schema_version == "1"`` on every record.
  3. ``PERSATRIX_LOG_FORMAT=pretty`` swaps the renderer (no JSON braces).
  4. ``NoopRedactor.redact()`` is invoked exactly once per record.
  5. Contextvars set inside a ``bind_contextvars`` scope appear in records and
     are cleared after.
  6. ``get_logger("test").info("event", k=1)`` round-trips through the chain.
  7. ``trace_id`` / ``span_id`` are emitted when an OTEL span is active and
     **omitted** when no span is in scope (per RFC 0018 § B Optional contract).
  8. ``service.kind`` / ``service.instance`` / ``service.role`` are bound at
     ``configure_logging()`` time and appear on every record.
"""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest
import structlog
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import set_tracer_provider

from agents.observability import logging as logging_mod
from agents.observability.logging import (
    _FIELD_ORDER,
    SCHEMA_VERSION,
    configure_logging,
    get_logger,
    set_redactor,
)
from agents.observability.redact import NoopRedactor, Redactor

# ─── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_structlog() -> Iterator[None]:
    """Reset structlog + module-level state between tests so configure_logging
    is exercised on every test rather than caching the first chain built."""
    # Save and clear contextvars so leftovers from one test do not leak.
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
    """Replace sys.stderr with a StringIO so the WriteLoggerFactory captures
    output for assertion."""
    buf = io.StringIO()
    monkeypatch.setattr("sys.stderr", buf)
    # Re-import module-level reference too, since configure_logging snapshots sys.stderr.
    monkeypatch.setattr(logging_mod, "sys", type("S", (), {"stderr": buf})())
    return buf


# ─── 1. Required-field set in documented order ──────────────────────────────


class TestSchemaShape:
    def test_required_fields_present(self, captured_stderr: io.StringIO) -> None:
        configure_logging(service_kind="agent", service_instance="ember-owl")
        get_logger("test").info("hello")

        line = captured_stderr.getvalue().strip().splitlines()[-1]
        record = json.loads(line)

        for required in (
            "schema_version",
            "timestamp",
            "level",
            "service.kind",
            "service.instance",
            "message",
        ):
            assert required in record, f"missing required field {required!r}"

    def test_field_emission_order(self, captured_stderr: io.StringIO) -> None:
        """Known fields are emitted in the documented order; unknowns appended."""
        configure_logging(service_kind="agent", service_instance="ember-owl")
        get_logger("test").info(
            "hello",
            execution_id="exec-1",
            extra_unknown="z",
            attributes={"k": "v"},
        )

        line = captured_stderr.getvalue().strip().splitlines()[-1]
        record = json.loads(line)
        keys = list(record.keys())

        # Filter the expected order to keys actually present in this record.
        present_known = [k for k in _FIELD_ORDER if k in record]
        # Known keys should appear in the documented order.
        prefix = keys[: len(present_known)]
        assert prefix == present_known, (
            f"known-field order mismatch:\n  want prefix: {present_known}\n  got: {keys}"
        )
        # Unknown key appears after the known set.
        assert keys[-1] == "extra_unknown"


# ─── 2. schema_version on every record ──────────────────────────────────────


class TestSchemaVersion:
    def test_present_on_info(self, captured_stderr: io.StringIO) -> None:
        configure_logging(service_kind="agent", service_instance="ember-owl")
        get_logger("test").info("hello")
        record = json.loads(captured_stderr.getvalue().strip().splitlines()[-1])
        assert record["schema_version"] == SCHEMA_VERSION
        assert SCHEMA_VERSION == "1"

    @pytest.mark.parametrize("level_name", ["debug", "info", "warning", "error"])
    def test_present_on_all_levels(
        self, captured_stderr: io.StringIO, level_name: str
    ) -> None:
        configure_logging(service_kind="agent", service_instance="ember-owl", level="DEBUG")
        getattr(get_logger("test"), level_name)("hello")
        line = captured_stderr.getvalue().strip().splitlines()[-1]
        record = json.loads(line)
        assert record["schema_version"] == SCHEMA_VERSION


# ─── 3. PERSATRIX_LOG_FORMAT=pretty selects console renderer ────────────────


class TestPrettyRenderer:
    def test_pretty_disables_json(
        self,
        monkeypatch: pytest.MonkeyPatch,
        captured_stderr: io.StringIO,
    ) -> None:
        monkeypatch.setenv("PERSATRIX_LOG_FORMAT", "pretty")
        configure_logging(service_kind="agent", service_instance="ember-owl")
        get_logger("test").info("hello", k="v")

        out = captured_stderr.getvalue()
        # Pretty renderer never emits a parseable top-level JSON object.
        # JSON renderer always starts every line with '{'.
        for line in out.strip().splitlines():
            with pytest.raises(json.JSONDecodeError):
                json.loads(line)

    def test_default_is_json(self, captured_stderr: io.StringIO) -> None:
        # PERSATRIX_LOG_FORMAT unset.
        configure_logging(service_kind="agent", service_instance="ember-owl")
        get_logger("test").info("hello")
        line = captured_stderr.getvalue().strip().splitlines()[-1]
        # Must parse as JSON.
        json.loads(line)


# ─── 4. Redactor invoked once per record ────────────────────────────────────


class _SpyRedactor:
    def __init__(self) -> None:
        self.calls = 0

    def redact(self, record: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        return record


class TestRedactorInvocation:
    def test_invoked_exactly_once_per_record(
        self, captured_stderr: io.StringIO
    ) -> None:
        spy = _SpyRedactor()
        # set_redactor must work *before* configure_logging too (module state).
        set_redactor(spy)
        configure_logging(service_kind="agent", service_instance="ember-owl")

        get_logger("test").info("a")
        get_logger("test").info("b")
        get_logger("test").info("c")

        assert spy.calls == 3

    def test_protocol_is_runtime_checkable(self) -> None:
        assert isinstance(NoopRedactor(), Redactor)
        assert isinstance(_SpyRedactor(), Redactor)


# ─── 5. Contextvars binding ─────────────────────────────────────────────────


class TestContextvars:
    def test_bound_vars_appear_in_record(self, captured_stderr: io.StringIO) -> None:
        configure_logging(service_kind="agent", service_instance="ember-owl")

        structlog.contextvars.bind_contextvars(execution_id="exec-42", step_id="step-1")
        get_logger("test").info("inside")
        structlog.contextvars.clear_contextvars()
        # Re-bind service.* manually since clear nukes everything; assert a
        # later record does NOT carry execution_id.
        get_logger("test").info("outside")

        lines = captured_stderr.getvalue().strip().splitlines()
        inside = json.loads(lines[-2])
        outside = json.loads(lines[-1])
        assert inside["execution_id"] == "exec-42"
        assert inside["step_id"] == "step-1"
        assert "execution_id" not in outside
        assert "step_id" not in outside


# ─── 6. Round-trip through chain ────────────────────────────────────────────


class TestRoundTrip:
    def test_kwargs_become_record_fields(self, captured_stderr: io.StringIO) -> None:
        configure_logging(service_kind="agent", service_instance="ember-owl")
        get_logger("test").info("event", k=1, nested={"a": "b"})

        record = json.loads(captured_stderr.getvalue().strip().splitlines()[-1])
        assert record["message"] == "event"
        assert record["k"] == 1
        assert record["nested"] == {"a": "b"}

    def test_event_is_renamed_to_message(self, captured_stderr: io.StringIO) -> None:
        configure_logging(service_kind="agent", service_instance="ember-owl")
        get_logger("test").info("hi there")
        record = json.loads(captured_stderr.getvalue().strip().splitlines()[-1])
        assert record["message"] == "hi there"
        assert "event" not in record

    def test_warning_normalises_to_warn(self, captured_stderr: io.StringIO) -> None:
        configure_logging(service_kind="agent", service_instance="ember-owl")
        get_logger("test").warning("careful")
        record = json.loads(captured_stderr.getvalue().strip().splitlines()[-1])
        assert record["level"] == "WARN"


# ─── 7. OTEL trace context ──────────────────────────────────────────────────


class TestOtelTraceContext:
    def test_omitted_when_no_span(self, captured_stderr: io.StringIO) -> None:
        configure_logging(service_kind="agent", service_instance="ember-owl")
        get_logger("test").info("no-span")

        record = json.loads(captured_stderr.getvalue().strip().splitlines()[-1])
        assert "trace_id" not in record
        assert "span_id" not in record

    def test_present_when_span_active(self, captured_stderr: io.StringIO) -> None:
        provider = TracerProvider()
        # Override global provider for the duration of the test.  pytest's
        # cleanup is sufficient: the next test re-asserts the no-span path.
        with patch("opentelemetry.trace.get_tracer_provider", return_value=provider):
            set_tracer_provider(provider)
            tracer = provider.get_tracer("test")
            configure_logging(service_kind="agent", service_instance="ember-owl")

            with tracer.start_as_current_span("op"):
                get_logger("test").info("with-span")

        record = json.loads(captured_stderr.getvalue().strip().splitlines()[-1])
        assert "trace_id" in record
        assert "span_id" in record
        assert len(record["trace_id"]) == 32  # hex-encoded 16 bytes
        assert len(record["span_id"]) == 16  # hex-encoded 8 bytes


# ─── 8. service.* binding at configure_logging() ────────────────────────────


class TestServiceBinding:
    def test_service_fields_bound(self, captured_stderr: io.StringIO) -> None:
        configure_logging(
            service_kind="agent", service_instance="ember-owl", service_role="persona"
        )
        get_logger("test").info("hi")
        get_logger("other").info("hi")  # different logger name; same service.* bind.

        for line in captured_stderr.getvalue().strip().splitlines():
            record = json.loads(line)
            assert record["service.kind"] == "agent"
            assert record["service.instance"] == "ember-owl"
            assert record["service.role"] == "persona"

    def test_service_role_omitted_when_unset(self, captured_stderr: io.StringIO) -> None:
        configure_logging(service_kind="orchestrator", service_instance="node-1")
        get_logger("test").info("hi")
        record = json.loads(captured_stderr.getvalue().strip().splitlines()[-1])
        assert "service.role" not in record
