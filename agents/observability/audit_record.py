"""One audit record, redacted, written as a structured log line.

Two parts of the runtime write audit lines that reach the orchestrator's log
stream but not its checksummed audit chain, since there is no Python-to-Go
audit call: the fact tier (:mod:`agents.memory._facts_audit`, RFC 0026 §G)
and a tool call the argument check refuses (:mod:`agents.tools.tool_list`,
RFC 0009 Phase 3). Both write through :func:`emit_audit_record`, so the
redaction and its fallback are one policy.

The registered redactor runs over the fields before the line is written,
even when :func:`~agents.observability.logging.configure_logging` has not
been called (unit tests, early start-up), before the structlog chain's own
redaction exists. A redactor that raises is reported in one WARNING on the
caller's logger, and the record then goes out unredacted, as
:meth:`agents.observability.redact.Redactor.redact` allows (PR #340 review
S2). A re-entry guard keeps that WARNING from recursing when a handler on
the logger writes an audit record of its own. A failure to log never
reaches the caller: an audit line must not break the work it records.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
from typing import Any

from .logging import get_redactor

__all__ = ["emit_audit_record"]

#: Set while the redactor-fallback WARNING is written, so an audit record
#: written from inside it (a handler that audits its own records) skips the
#: redactor instead of failing on it again. Separate from
#: :data:`agents.observability.logging._in_redactor_fallback`, the structlog
#: chain's own guard, so the two do not share state.
_in_redactor_fallback: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "_persatrix_audit_record_in_redactor_fallback", default=False,
)


def emit_audit_record(logger: logging.Logger, event: str, /, **fields: Any) -> None:
    """Write one ``audit=True`` record for *event*, carrying *fields*, on *logger*."""
    payload: dict[str, Any] = {"audit": True, **fields}
    if not _in_redactor_fallback.get():
        try:
            payload = get_redactor().redact(dict(payload))
        except Exception:  # noqa: BLE001 — the redactor is the operator's own callable.
            token = _in_redactor_fallback.set(True)
            try:
                with contextlib.suppress(Exception):
                    logger.warning(
                        "redactor raised; emitting unredacted audit record", exc_info=True,
                    )
            finally:
                _in_redactor_fallback.reset(token)
    with contextlib.suppress(Exception):
        logger.info(event, extra=payload)
