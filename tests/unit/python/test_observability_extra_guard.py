"""Guard tests for the stdlib ``extra=`` surfacing processor (ISSUE-0108 Gap A).

``agents/observability/logging.py``'s ``_surface_stdlib_extra`` lifts the
``extra=`` payload of the repo's audit convention (``agent.deliberated`` and the
``fact.*`` family, both emitted via stdlib ``logger.info(event, extra={...})``)
into the rendered JSON line — the egress an operator actually reads.

These tests assert at the *rendered* layer (not the ``caplog`` ``LogRecord``
layer, where the keys were always present and the original drop was invisible),
and they lock the processor's precedence contract that a bare
``structlog.stdlib.ExtraAdder`` does **not** provide: a caller's ``extra`` may
*fill a gap* but can **never overwrite** a chain-owned field —

  * schema machinery (``level`` / ``message`` / ``schema_version`` / …),
  * OTEL correlation IDs (``trace_id`` / ``span_id``), or
  * a *bound* contextvar identity (``agent_id`` / ``service.*`` / …).

Kept separate from ``test_observability_logging.py`` to keep that file under the
500-line review cap (``scripts/checks/file_size.py``).
"""

from __future__ import annotations

import io
import json
import logging as _stdlib_logging
from collections.abc import Iterator
from typing import Any

import pytest
import structlog

from agents.observability import logging as logging_mod
from agents.observability.logging import configure_logging, set_redactor
from agents.observability.redact import NoopRedactor

# ─── Fixtures (self-contained copies — mirror test_observability_logging.py) ──


@pytest.fixture(autouse=True)
def _reset_structlog() -> Iterator[None]:
    """Reset structlog + module-level state so configure_logging rebuilds its
    chain on every test rather than caching the first one built."""
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
    """Swap ``sys.stderr`` (and ``logging_mod.sys``, since the ``StreamHandler``
    binds the module's ``sys`` reference at build time) for a ``StringIO`` the
    handler installed by :func:`configure_logging` writes into."""
    import sys as _real_sys

    buf = io.StringIO()

    class _SysShim:
        stderr = buf

        def __getattr__(self, name: str) -> Any:  # pragma: no cover - trivial
            return getattr(_real_sys, name)

    monkeypatch.setattr("sys.stderr", buf)
    monkeypatch.setattr(logging_mod, "sys", _SysShim())
    return buf


# ─── Clobber guards ──────────────────────────────────────────────────────────


class TestExtraCannotClobber:
    def test_level_key_in_extra_does_not_change_level(
        self, captured_stderr: io.StringIO
    ) -> None:
        """``level`` is *not* a standard ``LogRecord`` attribute (the attribute is
        ``levelname``), so stdlib ``makeRecord`` lets ``extra={"level": ...}``
        through, and :func:`_normalise_level` *prefers* an existing ``level``
        key — a bare ``ExtraAdder`` would surface it and the ``info()`` record
        would render as ``ERROR``. The guard reserves ``level`` for the chain."""
        configure_logging(service_kind="agent", service_instance="ember-owl")
        _stdlib_logging.getLogger("agents.audit.example").info(
            "real-message", extra={"level": "ERROR", "reason_code": "kept"},
        )

        record = json.loads(captured_stderr.getvalue().strip().splitlines()[-1])
        assert record["level"] == "INFO"  # the method level, not the extra's
        assert record["reason_code"] == "kept"  # a non-colliding extra survives

    def test_bound_contextvar_identity_wins_over_extra(
        self, captured_stderr: io.StringIO
    ) -> None:
        """A bound contextvar identity (``agent_id`` / ``execution_id``) wins over
        a colliding ``extra`` key. ``merge_contextvars`` runs *before* the
        surfacing processor, so the trusted execution identity is already present
        and the guard never overwrites it. A bare ``ExtraAdder`` overwrote it,
        letting the audit payload's ``agent_id`` silently replace the
        trace-context identity (RFC 0018 PR 3) on every audit line."""
        configure_logging(service_kind="agent", service_instance="ember-owl")
        structlog.contextvars.bind_contextvars(
            agent_id="ctx-agent", execution_id="exec-ctx",
        )
        _stdlib_logging.getLogger("agents.audit.example").info(
            "agent.deliberated",
            extra={
                "agent_id": "payload-agent",  # collides with the contextvar
                "execution_id": "exec-payload",
                "reason_code": "novel_question",
            },
        )

        record = json.loads(captured_stderr.getvalue().strip().splitlines()[-1])
        assert record["agent_id"] == "ctx-agent"  # not "payload-agent"
        assert record["execution_id"] == "exec-ctx"  # not "exec-payload"
        assert record["reason_code"] == "novel_question"  # non-colliding survives

    def test_extra_cannot_inject_trace_context(
        self, captured_stderr: io.StringIO
    ) -> None:
        """``trace_id`` / ``span_id`` are OTEL-owned and must never come from
        ``extra`` — otherwise a foreign record could forge correlation when no
        span is active (``_add_otel_trace_context`` only sets them under a live
        span, so a bare ``ExtraAdder`` value would survive)."""
        configure_logging(service_kind="agent", service_instance="ember-owl")
        _stdlib_logging.getLogger("agents.audit.example").info(
            "an.audit.event",
            extra={"trace_id": "deadbeef" * 4, "span_id": "feed" * 4, "ok": 1},
        )

        record = json.loads(captured_stderr.getvalue().strip().splitlines()[-1])
        assert "trace_id" not in record  # no active span → not injectable
        assert "span_id" not in record
        assert record["ok"] == 1  # a non-reserved extra still surfaces


# ─── Scope: third-party foreign records are left untouched ───────────────────


class TestThirdPartyNotSurfaced:
    def test_third_party_record_extras_are_not_surfaced(
        self, captured_stderr: io.StringIO
    ) -> None:
        """The ``extra=`` audit convention is *ours*. A third-party library
        record (``grpc`` / ``asyncio`` / …) must NOT have its attributes surfaced.

        Beyond scope-correctness this is a CI-hang guard: a bare
        ``structlog.stdlib.ExtraAdder`` surfaced third-party extras, which then
        flowed to the log shipper's ``Struct`` conversion while the shipper's own
        error path re-enqueued — a feedback loop that hung the real-shipper
        startup tests under a dead orchestrator. The surfacing processor is scoped
        to our application logger roots, so a ``grpc`` record renders exactly as it
        did before ISSUE-0108 (no surfaced extras)."""
        configure_logging(service_kind="agent", service_instance="ember-owl")
        _stdlib_logging.getLogger("grpc._channel").info(
            "connection-failed", extra={"peer": "127.0.0.1:9090", "reason_code": "x"},
        )

        record = json.loads(captured_stderr.getvalue().strip().splitlines()[-1])
        assert record["message"] == "connection-failed"  # the record still renders
        assert "peer" not in record  # third-party extra is NOT surfaced
        assert "reason_code" not in record  # ... not even a schema-shaped key


# ─── Gap-fill (the deliberate other half of the precedence rule) ─────────────


class TestExtraFillsGap:
    def test_extra_fills_identity_when_no_contextvar_bound(
        self, captured_stderr: io.StringIO
    ) -> None:
        """When no contextvar identity is bound, an ``extra`` value *fills* it.

        A ``fact.*`` audit emitted outside a gRPC request (early startup, CLI,
        migrations) has no contextvar ``agent_id``, so the audit's own
        ``agent_id`` must still reach the line. The salience-gate deliberation
        audit relies on exactly this path in its rendered-egress test."""
        configure_logging(service_kind="agent", service_instance="ember-owl")
        # configure_logging binds service.* only — no agent_id contextvar.
        _stdlib_logging.getLogger("agents.audit.example").info(
            "fact.store", extra={"agent_id": "payload-agent", "fact_id": "f-1"},
        )

        record = json.loads(captured_stderr.getvalue().strip().splitlines()[-1])
        assert record["agent_id"] == "payload-agent"  # gap-filled from extra
        assert record["fact_id"] == "f-1"


# ─── fact.* rendered egress (the other half of Gap A) ────────────────────────


class TestFactAuditRenderedEgress:
    def test_fact_payload_reaches_rendered_line_and_is_redacted(
        self, captured_stderr: io.StringIO
    ) -> None:
        """The ``fact.*`` family uses the same stdlib ``extra=`` audit convention,
        so its payload was equally dropped from the rendered line. Drive the real
        write surface (:func:`agents.memory._facts_audit.emit_audit`) through the
        renderer and assert the full ``fact.store`` payload — including the
        subject/predicate/object triple — reaches the JSON, and that the triple
        still flows through the redactor (it can carry user content, so its egress
        must be redactable, not silent)."""
        from agents.memory._facts_audit import emit_audit

        class _ObjectMaskingRedactor:
            def redact(self, record: dict[str, Any]) -> dict[str, Any]:
                if "object" in record:
                    record["object"] = "***"
                return record

        configure_logging(service_kind="agent", service_instance="ember-owl")
        set_redactor(_ObjectMaskingRedactor())
        emit_audit(
            "fact.store", agent_id="ember-owl", fact_id="f-1",
            subject="Alice", predicate="favourite_colour", object="green",
            source_interaction_id="int-9",
        )

        # Select the fact.store line explicitly — the fact.store piggyback
        # (emit_for_tier) runs after the audit emit and could append a line.
        rendered = [
            json.loads(line)
            for line in captured_stderr.getvalue().strip().splitlines()
            if line.startswith("{")
        ]
        record = next(r for r in rendered if r.get("message") == "fact.store")
        assert record["audit"] is True
        assert record["subject"] == "Alice"
        assert record["predicate"] == "favourite_colour"
        assert record["fact_id"] == "f-1"
        assert record["object"] == "***"  # redacted, not the raw "green"
        assert "green" not in captured_stderr.getvalue()
