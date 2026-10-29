"""Audit lines for tool calls (RFC 0009 Phase 3).

A tool call the agent refuses, such as one whose arguments fail the check in
:mod:`agents.tools.arg_check` (``tool.arg_invalid``), never runs, so no tool
span or metric records it; this module writes the audit line that does. As
the RFC 0009 Phases 3–4 plan's G-4 says, it is a structured log record, the
pattern :mod:`agents.memory._facts_audit` uses: it reaches the
orchestrator's log stream but not its checksummed audit chain, since there is
no Python-to-Go audit call.

The registered redactor runs over the fields before the line is written,
even before logging is configured, and a failure to log never fails the
call: a redactor that raises is reported in a WARNING and the fields go out
unredacted, as :meth:`agents.observability.redact.Redactor.redact` allows.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from ..observability.logging import get_redactor

__all__ = ["emit_tool_audit"]

_logger = logging.getLogger("agents.tools.audit")


def emit_tool_audit(event: str, **fields: Any) -> None:
    """Write one ``audit=True`` record for *event* carrying *fields*."""
    payload: dict[str, Any] = {"audit": True, **fields}
    try:
        payload = get_redactor().redact(dict(payload))
    except Exception:  # noqa: BLE001 — the redactor is the operator's own callable.
        with contextlib.suppress(Exception):
            _logger.warning("redactor raised; writing an unredacted audit line", exc_info=True)
    with contextlib.suppress(Exception):
        _logger.info(event, extra=payload)
