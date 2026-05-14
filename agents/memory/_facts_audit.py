"""Audit-log emission for :class:`agents.memory.facts.FactStore`
(RFC 0026 §G).

Split out of :mod:`agents.memory.facts` so the parent module stays
under the project's 500-line review-friendly cap (see
``scripts/checks/file_size.py``).  The emission helper is the **single
write surface** for fact-tier audit records; both ``store`` (with its
optional supersede branch) and ``supersede`` route through here so the
redaction policy + record-shape stay consistent.

Redaction
---------
The registered :class:`~agents.observability.redact.Redactor` is
invoked on every payload before emission, even when
:func:`~agents.observability.logging.configure_logging` has not been
called — important for the unit-test environment and early-startup
code where the structlog chain has not yet been built.  RFC 0026 §G
names ``RedactStruct`` (Go) as the cross-language policy anchor; this
helper is the Python-side counterpart.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from ..observability.logging import get_redactor

__all__ = ["emit_audit"]

_logger = logging.getLogger("agents.memory.facts")


def emit_audit(event: str, **fields: Any) -> None:
    """Emit one ``audit=True`` record carrying ``event`` + ``fields``.

    Failures in the redactor / logger are swallowed — a structured-log
    hiccup must never break a write that has already committed.
    """
    payload: dict[str, Any] = {"audit": True, **fields}
    with contextlib.suppress(Exception):
        payload = get_redactor().redact(dict(payload))
    with contextlib.suppress(Exception):
        _logger.info(event, extra=payload)
