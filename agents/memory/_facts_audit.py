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
The record is written by
:func:`agents.observability.audit_record.emit_audit_record`, which the
tool argument check's audit line shares: it runs the registered
:class:`~agents.observability.redact.Redactor` before the structlog
chain exists, reports a redactor that raises in one out-of-band
WARNING on this module's logger (PR #340 review S2), and guards that
WARNING against re-entry.  RFC 0026 §G names ``RedactStruct`` (Go) as
the cross-language policy anchor; that helper is the Python-side
counterpart.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from ..observability.audit_record import emit_audit_record
from ._salience import FACTS_APPEND_SALIENCE, emit_for_tier

__all__ = ["emit_audit"]

_logger = logging.getLogger("agents.memory.facts")


def emit_audit(event: str, **fields: Any) -> None:
    """Emit one ``audit=True`` record carrying ``event`` + ``fields``.

    Failures in the logger are swallowed — a structured-log hiccup
    must never break a write that has already committed (see
    :func:`~agents.observability.audit_record.emit_audit_record`).
    """
    emit_audit_record(_logger, event, **fields)
    # RFC 0024 PR 3a — every successful fact-store commit routes through
    # ``_emit_audit("fact.store", ...)`` exactly once.  Piggyback the
    # memory-write event here so :mod:`agents.memory.facts` does not need
    # its own emit call (keeps it under the 500-line review cap).
    #
    # Coverage scope: ONLY ``fact.store`` carries the piggyback.  Sibling
    # audit events emitted from the facts tier — ``fact.recalled`` (read-
    # path reinforcement, :mod:`._facts_reinforce`), ``fact.supersede``
    # (metadata-only retraction, :meth:`facts.FactStore.supersede` and the
    # supersede branch of :meth:`facts.FactStore.store`) — are deliberately
    # NOT lifted as memory-write events: a recall or supersede is not a
    # *new* tier write for salience purposes, and double-emitting on the
    # supersede branch of ``store`` would violate the "exactly one event
    # per write" contract pinned by ``test_memory_write_event``.
    if event == "fact.store":
        agent_id = fields.get("agent_id")
        if isinstance(agent_id, str):
            emit_for_tier(agent_id=agent_id, tier="facts",
                          salience=FACTS_APPEND_SALIENCE)
        else:
            # Programmer-error path: a future ``fact.store`` audit caller
            # that forgets ``agent_id=`` (or passes a non-string) would
            # otherwise silently drop the memory-write event with zero
            # signal, breaking PR 3b's salience-wake coverage for the
            # facts tier.  WARNING (not raise) preserves the failure-
            # isolation contract — the row is already committed by the
            # time this branch executes.
            with contextlib.suppress(Exception):
                _logger.warning(
                    "fact.store audit missing or non-string agent_id; "
                    "MemoryWriteEvent for facts tier was not emitted "
                    "(RFC 0024 PR 3a piggyback contract)",
                )
