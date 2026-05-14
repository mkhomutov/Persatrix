"""Persatrix structured-logging configuration (RFC 0018 Phase 1).

This module is the Python-side entry point for the v0.2.3 observability
foundation.  It builds a ``structlog`` processor chain that emits the
versioned JSON line schema documented in :doc:`docs/observability.md`
(``schema_version: "1"``).

Public surface
--------------
* :func:`configure_logging` — build the chain once at process start.
* :func:`get_logger`        — return a structlog ``BoundLogger`` for a module.
* :func:`set_redactor`      — install a :class:`~agents.observability.redact.Redactor`
                              implementation (defaults to :class:`NoopRedactor`).

Environment variables
---------------------
``PERSATRIX_LOG_FORMAT``
    ``json`` (default) or ``pretty``.  Pretty selects ``structlog.dev.ConsoleRenderer``
    for human-readable local development; the default is the JSON wire format
    consumed by the future ``persatrix logs`` endpoint (RFC 0018 Phase 4).

Stdlib bridge
-------------
``get_logger`` returns a ``structlog.stdlib.BoundLogger`` backed by
``structlog.stdlib.LoggerFactory``.  Every ``logger.info(...)`` call
ultimately emits a stdlib ``LogRecord`` rendered by ``ProcessorFormatter`` on
the root handler.  Two consequences:

1. ``pytest``'s ``caplog`` fixture captures records from named loggers exactly
   as it did with stdlib ``logging.getLogger`` — the existing
   ``test_persona_tick_shortcircuit.py`` continues to work without rewrites.
2. Third-party libraries that emit through stdlib ``logging`` (grpc, anthropic,
   openai) flow through the same ``ProcessorFormatter`` and are rendered in the
   same JSON schema, so a single CLI consumer sees one wire format.

Cross-RFC coupling
------------------
* The OTEL processor reads ``opentelemetry.trace.get_current_span()`` (the
  OTEL provider is initialised by RFC 0019 Phase 1, see
  :mod:`agents.observability.tracing`).  When no span is active or its
  context is invalid the ``trace_id`` / ``span_id`` keys are **omitted**
  (not emitted as empty strings) — this preserves the schema's "Optional"
  contract from RFC 0018 § B.
* The redactor hook is the same shape RFC 0019 Phase 2 will call for opt-in
  tool-payload capture as span attributes.
"""

from __future__ import annotations

import contextvars
import logging as _stdlib_logging
import os
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog
from opentelemetry import trace

from .redact import NoopRedactor, Redactor

# ─── Schema constants (RFC 0018 § B) ─────────────────────────────────────────

#: Schema version emitted on every record.  Bumping this is a breaking change.
SCHEMA_VERSION = "1"

#: Allowed ``service.kind`` values per RFC 0018 § B.  Validated in
#: :func:`configure_logging` so that schema conformance is enforced at the
#: process boundary rather than discovered later by a downstream consumer
#: that branches on the documented enum (PR #164 review — Should Fix #2).
_VALID_SERVICE_KINDS: frozenset[str] = frozenset({"orchestrator", "agent", "cli"})

#: Allowed log levels per RFC 0018 § B.  ``WARNING`` is also accepted on input
#: as an ergonomic alias for ``WARN`` (matches the stdlib + existing
#: ``--log-level`` CLI choices in :mod:`agents.server`); on the wire we always
#: emit ``WARN`` via :func:`_normalise_level`.
_VALID_LEVELS: frozenset[str] = frozenset({"DEBUG", "INFO", "WARN", "WARNING", "ERROR"})

#: Required-then-optional emission order from RFC 0018 § B.  The
#: ``_reorder_keys`` processor at the tail of the chain emits keys in this
#: order; any unknown keys are appended in insertion order after the known set.
_FIELD_ORDER: tuple[str, ...] = (
    # Required (RFC § B table 1)
    "schema_version",
    "timestamp",
    "level",
    "service.kind",
    "service.instance",
    "message",
    # Optional (RFC § B table 2)
    "service.role",
    "execution_id",
    "step_id",
    "agent_id",
    "workflow_id",
    "request_id",
    "trace_id",
    "span_id",
    "attributes",
    "source",
)

# ─── Module-level state ──────────────────────────────────────────────────────

_redactor: Redactor = NoopRedactor()
_configured: bool = False

#: Re-entry guard for :func:`_apply_redactor`'s fallback warning.  Without
#: this, emitting a warning when the redactor raises would itself flow
#: through the same processor chain (including ``_apply_redactor``) and
#: re-trigger the same exception, producing unbounded recursion.  Using a
#: :class:`~contextvars.ContextVar` keeps the guard async-task-local rather
#: than process-global (PR #164 review — Must Fix #1, follow-up).
_in_redactor_fallback: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "_persatrix_in_redactor_fallback", default=False
)


def set_redactor(redactor: Redactor) -> None:
    """Install a :class:`~agents.observability.redact.Redactor`.

    Safe to call before or after :func:`configure_logging`; the chain reads
    the module-level ``_redactor`` at every record emission.
    """
    global _redactor
    _redactor = redactor


def get_redactor() -> Redactor:
    """Return the currently-installed :class:`Redactor`.

    Exposed for callers that need to redact a structured payload outside
    the structlog chain — e.g. RFC 0026 audit emissions in
    :mod:`agents.memory.facts` build a dict, run it through the same
    redactor the chain would apply, then hand it to ``logger.info``
    so PII is scrubbed even before :func:`configure_logging` has been
    called (test environments, early-startup code).
    """
    return _redactor


# ─── Processors ──────────────────────────────────────────────────────────────


def _rename_event_to_message(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog stores the positional message under ``event``; the schema
    calls it ``message``.  Rename in place."""
    if "event" in event_dict:
        event_dict["message"] = event_dict.pop("event")
    return event_dict


def _add_schema_version(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    event_dict["schema_version"] = SCHEMA_VERSION
    return event_dict


def _normalise_level(
    _logger: Any, method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Schema requires upper-case levels (``DEBUG`` / ``INFO`` / ``WARN`` /
    ``ERROR``); structlog emits lower-case method names by default and uses
    ``warning`` rather than ``warn``."""
    raw = event_dict.pop("level", method_name).upper()
    event_dict["level"] = "WARN" if raw == "WARNING" else raw
    return event_dict


def _add_otel_trace_context(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Add ``trace_id`` / ``span_id`` when an OTEL span is active.

    Per RFC 0018 § B (Optional fields), absent fields are *omitted*, not
    emitted as empty strings.  ``get_current_span()`` returns an
    ``INVALID_SPAN`` sentinel when no span is in scope; we filter on
    ``span_context.is_valid`` rather than truthiness of the IDs.
    """
    span = trace.get_current_span()
    span_context = span.get_span_context() if span is not None else None
    if span_context is not None and span_context.is_valid:
        event_dict["trace_id"] = format(span_context.trace_id, "032x")
        event_dict["span_id"] = format(span_context.span_id, "016x")
    return event_dict


def _apply_redactor(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Invoke the registered :class:`Redactor` exactly once per record.

    The redactor receives a plain ``dict`` snapshot and may return either the
    same object or a new one.

    A buggy or hostile redactor that raises must not take down every
    ``logger.info(...)`` call in the process — the documented contract on
    :meth:`agents.observability.redact.Redactor.redact` promises that errors
    surface as the *unredacted* record being emitted with an out-of-band
    warning.  We honour that contract here (PR #164 review — Must Fix #1) by
    catching ``Exception`` and falling back to ``event_dict``.  ``BLE001`` is
    silenced because this is the deliberate last line of defence around an
    arbitrary user-supplied callable.

    The warning is emitted via the **stdlib** logger; a contextvar guard
    (:data:`_in_redactor_fallback`) skips the redactor on the warning record
    itself, otherwise the warning would re-enter the chain and re-trigger
    the same exception (unbounded recursion).
    """
    if _in_redactor_fallback.get():
        # We are inside the fallback warning emission; do not invoke the
        # (broken) redactor again.
        return event_dict
    try:
        return _redactor.redact(dict(event_dict))
    except Exception:  # noqa: BLE001 — see docstring; deliberate broad catch.
        token = _in_redactor_fallback.set(True)
        try:
            _stdlib_logging.getLogger("agents.observability.logging").warning(
                "redactor raised; emitting unredacted record", exc_info=True,
            )
        finally:
            _in_redactor_fallback.reset(token)
        return event_dict


def _reorder_keys(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Emit keys in the documented schema order; unknown keys are appended."""
    ordered: dict[str, Any] = {}
    for key in _FIELD_ORDER:
        if key in event_dict:
            ordered[key] = event_dict[key]
    for key, value in event_dict.items():
        if key not in ordered:
            ordered[key] = value
    return ordered


def _ship_to_orchestrator(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Enqueue the (post-reorder) record onto the active log shipper.

    No-op when no shipper is installed (the common case at process
    startup before :func:`agents.observability.log_shipper.set_active_shipper`
    runs, and during tests).  Imported lazily so :mod:`logging` does
    not pull in :mod:`grpc` for callers that never start the shipper
    (e.g. unit tests).

    Failures are swallowed and counted only by the shipper itself —
    the structlog chain must never raise.
    """
    try:
        from .log_shipper import get_active_shipper
    except Exception:  # noqa: BLE001 — never block local emission.
        return event_dict
    shipper = get_active_shipper()
    if shipper is None:
        return event_dict
    try:
        shipper.enqueue(dict(event_dict))
    except Exception:  # noqa: BLE001 — same reason as above.
        pass
    return event_dict


def _build_processors() -> list[structlog.types.Processor]:
    """Shared processor chain — used both by ``structlog.configure()`` (for
    structlog-native callers) and by ``ProcessorFormatter.foreign_pre_chain``
    (for stdlib callers).
    """
    return [
        # 1. Per-async-task contextvars (execution_id / step_id / agent_id
        #    bound by the gRPC interceptor in RFC 0018 PR 3).
        structlog.contextvars.merge_contextvars,
        # 2. Schema-required fields.
        _add_schema_version,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _normalise_level,
        _rename_event_to_message,
        # 3. OTEL trace context (RFC 0019 Phase 1 dependency).
        _add_otel_trace_context,
        # 4. Redaction hook (RFC 0018 § F).
        _apply_redactor,
        # 5. Schema field order.
        _reorder_keys,
        # 6. Fan out to the orchestrator log shipper (RFC 0018 PR 5).
        #    No-op when no shipper is installed (tests, CLI).
        _ship_to_orchestrator,
    ]


def _select_renderer() -> structlog.types.Processor:
    """``json`` (default) or ``pretty`` per ``PERSATRIX_LOG_FORMAT``."""
    fmt = os.environ.get("PERSATRIX_LOG_FORMAT", "json").lower()
    if fmt == "pretty":
        return structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    return structlog.processors.JSONRenderer()


# ─── Public API ──────────────────────────────────────────────────────────────


def configure_logging(
    *,
    service_kind: str = "agent",
    service_instance: str | None = None,
    service_role: str | None = None,
    level: str = "INFO",
) -> None:
    """Configure structlog + stdlib bridge for the current process.

    Idempotent: subsequent calls update the bound ``service.*`` context but
    do not rebuild the processor chain.  As a consequence,
    ``PERSATRIX_LOG_FORMAT`` is read **once on the first call** and the
    resulting renderer (JSON or pretty) is frozen for the remainder of the
    process.  Re-exporting the env var between two ``configure_logging``
    calls has no effect (PR #164 review — Should Fix #4).

    Parameters
    ----------
    service_kind
        ``orchestrator`` / ``agent`` / ``cli`` per RFC 0018 § B.  Validated
        against the documented enum; an unknown value raises ``ValueError``
        rather than silently emitting a non-conformant record.
    service_instance
        Process identity (e.g. agent ID).  Falls back to
        ``PERSATRIX_AGENT_ID`` then to ``"unknown"``.
    service_role
        Optional persona / agent role (e.g. ``coder``, ``reviewer``).
    level
        Log level name (``DEBUG`` / ``INFO`` / ``WARN`` / ``WARNING`` /
        ``ERROR``).  ``WARNING`` is accepted as an ergonomic alias for
        ``WARN``; the wire format always emits ``WARN``.

    Raises
    ------
    ValueError
        If ``service_kind`` is not in :data:`_VALID_SERVICE_KINDS` or
        ``level`` is not in :data:`_VALID_LEVELS`.  Fail-fast keeps schema
        conformance enforceable at the boundary rather than aspirational
        (PR #164 review — Should Fix #2).
    """
    global _configured

    # Validate against the documented schema enums *before* any side-effect.
    if service_kind not in _VALID_SERVICE_KINDS:
        raise ValueError(
            f"service_kind={service_kind!r} not in {sorted(_VALID_SERVICE_KINDS)} "
            f"(RFC 0018 § B)"
        )
    level_upper = level.upper()
    if level_upper not in _VALID_LEVELS:
        raise ValueError(
            f"level={level!r} not in {sorted(_VALID_LEVELS)} (RFC 0018 § B)"
        )

    instance = service_instance or os.environ.get("PERSATRIX_AGENT_ID", "unknown")
    stdlib_level_name = "WARNING" if level_upper == "WARN" else level_upper
    stdlib_level = getattr(_stdlib_logging, stdlib_level_name, _stdlib_logging.INFO)

    if _configured:
        # Re-bind service.* context; chain is already built.
        _bind_service_context(service_kind, instance, service_role)
        _stdlib_logging.getLogger().setLevel(stdlib_level)
        return

    shared_processors = _build_processors()

    # ── structlog side ────────────────────────────────────────────────
    structlog.configure(
        processors=[
            *shared_processors,
            # Hand off to the stdlib root handler's ProcessorFormatter.
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(stdlib_level),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # ── stdlib root handler with ProcessorFormatter ──────────────────
    root = _stdlib_logging.getLogger()
    root.setLevel(stdlib_level)
    # Remove existing handlers so we don't double-emit if some module called
    # ``logging.basicConfig`` earlier in the process lifetime.
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = structlog.stdlib.ProcessorFormatter(
        # foreign_pre_chain runs for records that came in via stdlib
        # ``logging.getLogger().info("msg")`` (third-party libs).  It applies
        # the schema processors so foreign records also conform.
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            _select_renderer(),
        ],
    )
    handler = _stdlib_logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(formatter)
    root.addHandler(handler)

    _bind_service_context(service_kind, instance, service_role)
    _configured = True


def _bind_service_context(
    service_kind: str, service_instance: str, service_role: str | None
) -> None:
    """Bind ``service.*`` context to the structlog contextvars set."""
    ctx: dict[str, Any] = {
        "service.kind": service_kind,
        "service.instance": service_instance,
    }
    if service_role:
        ctx["service.role"] = service_role
    structlog.contextvars.bind_contextvars(**ctx)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a structlog ``BoundLogger`` for the given module name.

    Mirrors the stdlib ``logging.getLogger(__name__)`` ergonomics so the
    PR 1 swap is mechanical.  When :func:`configure_logging` has not been
    called the logger still works (structlog falls back to its built-in
    defaults), but records will not carry the schema fields.
    """
    return structlog.get_logger(name)  # type: ignore[no-any-return]


__all__ = [
    "SCHEMA_VERSION",
    "configure_logging",
    "get_logger",
    "get_redactor",
    "set_redactor",
]
