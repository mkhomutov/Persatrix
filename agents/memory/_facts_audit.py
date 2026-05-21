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

Redactor failure observability (PR #340 review S2)
--------------------------------------------------
The chain's :func:`agents.observability.logging._apply_redactor`
processor emits an out-of-band WARNING when the registered redactor
raises (PR #164 review Must-Fix #1).  That path runs after
``configure_logging`` has built the chain.  This helper runs the
redactor **before** that chain — for unit tests, early startup, and
any process that never calls ``configure_logging``.  Without an
explicit warning here, a misconfigured redactor in that pre-chain
window silently passes raw PII to the audit sink with zero signal.

The fallback mirrors the chain: catch the exception, emit one
WARNING via stdlib logging, then proceed with the unredacted payload
(the contract on :meth:`agents.observability.redact.Redactor.redact`
explicitly allows this — errors surface as the *unredacted* record
being emitted with an out-of-band warning).  A contextvar guard
prevents the warning from re-entering this same helper if a caller
ever installs a redactor that records its own warnings via
``emit_audit``.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
from typing import Any

from ..observability.logging import get_redactor
from ._salience import FACTS_APPEND_SALIENCE, emit_for_tier

__all__ = ["emit_audit"]

_logger = logging.getLogger("agents.memory.facts")

#: Re-entry guard for the redactor-fallback WARNING.  Without this,
#: a hostile / misconfigured redactor that itself calls back into
#: :func:`emit_audit` (e.g. via a logging handler that audits its own
#: emissions) would recurse on every failure.  Mirrors the
#: :data:`agents.observability.logging._in_redactor_fallback` pattern
#: but lives on this module's logger so the two guards do not share
#: state (each redactor invocation owns its own re-entry semantics).
_in_redactor_fallback: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "_persatrix_facts_audit_in_redactor_fallback", default=False,
)


def emit_audit(event: str, **fields: Any) -> None:
    """Emit one ``audit=True`` record carrying ``event`` + ``fields``.

    Failures in the logger are swallowed — a structured-log hiccup
    must never break a write that has already committed.  Redactor
    failures are caught here and surface as an out-of-band WARNING on
    the same logger (see module docstring) before the unredacted
    record is emitted; this matches the structlog chain's contract on
    :meth:`agents.observability.redact.Redactor.redact`.
    """
    payload: dict[str, Any] = {"audit": True, **fields}
    if not _in_redactor_fallback.get():
        try:
            payload = get_redactor().redact(dict(payload))
        except Exception:  # noqa: BLE001 — last line of defence around user-supplied callable.
            token = _in_redactor_fallback.set(True)
            try:
                with contextlib.suppress(Exception):
                    _logger.warning(
                        "redactor raised; emitting unredacted audit record",
                        exc_info=True,
                    )
            finally:
                _in_redactor_fallback.reset(token)
    with contextlib.suppress(Exception):
        _logger.info(event, extra=payload)
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
