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
    """Replace ``sys.stderr`` with a ``StringIO`` so the ``StreamHandler``
    installed by :func:`configure_logging` captures output for assertion.

    ``configure_logging`` constructs ``logging.StreamHandler(stream=sys.stderr)``
    where ``sys`` is ``logging_mod``'s import-bound reference.  Pytest's own
    capture machinery defeats a plain ``monkeypatch.setattr("sys.stderr", buf)``
    for handlers built *after* the patch, so we additionally swap
    ``logging_mod.sys`` for a thin shim whose ``stderr`` is ``buf``.

    PR #164 review — Should Fix #3 flagged that the previous shim exposed
    *only* ``stderr`` and would ``AttributeError`` on any future processor /
    renderer code touching ``sys.platform``, ``sys.exc_info`` etc.  The shim
    below forwards every other attribute lookup to the real ``sys`` module
    via ``__getattr__``, making it robust to such future edits while keeping
    the capture mechanism intact.
    """
    import sys as _real_sys

    buf = io.StringIO()

    class _SysShim:
        stderr = buf

        def __getattr__(self, name: str) -> Any:  # pragma: no cover - trivial
            return getattr(_real_sys, name)

    monkeypatch.setattr("sys.stderr", buf)
    monkeypatch.setattr(logging_mod, "sys", _SysShim())
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


# ─── 9. Foreign stdlib bridge (PR #164 review — Should Fix #1) ──────────────
#
# The descope of PR 1b (mechanical ``import logging`` → ``get_logger`` swap)
# rests entirely on the claim that records emitted via stdlib
# ``logging.getLogger("third_party").info(...)`` already flow through the
# schema chain via ``ProcessorFormatter.foreign_pre_chain``.  Lock that
# contract with a test so the descope rationale is machine-checked rather
# than only validated by hand.


class TestForeignStdlibBridge:
    def test_stdlib_record_carries_schema_fields(
        self, captured_stderr: io.StringIO
    ) -> None:
        import logging as _stdlib_logging

        configure_logging(service_kind="agent", service_instance="ember-owl")
        # Mimic what grpc / openai / anthropic do today.
        _stdlib_logging.getLogger("third_party.lib").info("foreign-record")

        line = captured_stderr.getvalue().strip().splitlines()[-1]
        record = json.loads(line)
        # All required schema fields must be present on a foreign record.
        assert record["schema_version"] == SCHEMA_VERSION
        assert record["service.kind"] == "agent"
        assert record["service.instance"] == "ember-owl"
        assert record["level"] == "INFO"
        assert record["message"] == "foreign-record"
        assert "timestamp" in record


# ─── 10. Raising redactor falls back (PR #164 review — Must Fix #1) ─────────
#
# ``redact.py`` documents that errors surface as the unredacted record being
# emitted with an out-of-band warning.  Lock that contract.


class _BoomRedactor:
    def redact(self, record: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("boom")


class TestRaisingRedactor:
    def test_record_still_emitted_when_redactor_raises(
        self, captured_stderr: io.StringIO
    ) -> None:
        set_redactor(_BoomRedactor())
        configure_logging(service_kind="agent", service_instance="ember-owl")

        # Must not raise out of ``logger.info``.
        get_logger("test").info("survives", k="v")

        # The original (unredacted) record is still on the wire.
        # The fallback warning may also appear; pick the line carrying our
        # message rather than the last line.
        records = [
            json.loads(line)
            for line in captured_stderr.getvalue().strip().splitlines()
            if line.startswith("{")
        ]
        survivors = [r for r in records if r.get("message") == "survives"]
        assert survivors, "original record was dropped when redactor raised"
        assert survivors[-1]["k"] == "v"


# ─── 11. Enum validation (PR #164 review — Should Fix #2) ───────────────────


class TestEnumValidation:
    def test_invalid_service_kind_raises(self) -> None:
        with pytest.raises(ValueError, match="service_kind"):
            configure_logging(service_kind="Agent", service_instance="x")

    def test_invalid_level_raises(self) -> None:
        with pytest.raises(ValueError, match="level"):
            configure_logging(
                service_kind="agent", service_instance="x", level="VERBOSE"
            )

    def test_warning_alias_accepted(self, captured_stderr: io.StringIO) -> None:
        # ``--log-level WARNING`` is the existing CLI default in agents/server.py;
        # it must remain accepted even though the wire format is ``WARN``.
        configure_logging(
            service_kind="agent", service_instance="x", level="WARNING"
        )
        get_logger("test").warning("hi")
        record = json.loads(captured_stderr.getvalue().strip().splitlines()[-1])
        assert record["level"] == "WARN"
