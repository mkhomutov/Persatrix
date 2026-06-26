"""Surface the stdlib ``extra=`` dict of *our own* foreign records (ISSUE-0108).

Split out of :mod:`agents.observability.logging` so the parent module stays
under the project's 500-line review cap (see ``scripts/checks/file_size.py``).

Background
----------
The repo's audit convention emits structured payloads via the stdlib idiom
``logger.info(event, extra={...})`` — ``agent.deliberated``
(:mod:`agents.persona_runtime.salience_gate`) and the ``fact.*`` family
(:mod:`agents.memory._facts_audit`). ``ProcessorFormatter`` renders foreign
(stdlib) records through ``foreign_pre_chain``; without a processor that copies
the ``extra`` keys, those payloads were dropped to a presence-only line.

Why not a bare ``structlog.stdlib.ExtraAdder``
----------------------------------------------
``ExtraAdder`` copies **every** non-standard attribute of **every** foreign
record and *overwrites* whatever the chain already set. That is wrong on two
axes:

1. **Clobbering.** A colliding ``extra`` key would replace a chain-owned field —
   ``level`` (re-derived by ``_normalise_level``, which prefers an existing
   ``level``), the OTEL ``trace_id`` / ``span_id`` when no span is live, or a
   contextvar-bound identity (``agent_id`` / ``service.*``) merged just above.

2. **Third-party blast radius (and a CI hang).** The audit ``extra=`` convention
   is *ours*. A bare adder also surfaces attributes of **third-party** records —
   ``grpc`` (the log shipper's own channel, hammering a dead orchestrator in
   tests), ``asyncio``, ``anthropic`` / ``openai``. Those attributes can be
   arbitrary non-serialisable objects, and once surfaced they flow to the log
   shipper's ``record_to_proto`` → ``Struct`` conversion *and* the shipper's
   error path re-logs via the same chain, re-enqueuing into its own queue — a
   feedback loop that wedged the real-shipper startup tests
   (``TestStartupCatchUpWiring``) into a multi-minute CI hang. Scoping surfacing
   to our own records makes third-party records render exactly as they did
   before ISSUE-0108 (no surfaced extras), which is the safe, intended behaviour.

:func:`surface_stdlib_extra` therefore surfaces a key only when the record is one
of *ours* **and** the key is neither reserved (chain-owned) nor already present
(a bound contextvar/service value wins; ``extra`` only ever fills a gap).
"""

from __future__ import annotations

import logging as _stdlib_logging
from collections.abc import MutableMapping
from typing import Any

__all__ = ["surface_stdlib_extra"]

#: Standard ``LogRecord`` attribute names; a key on a foreign record's
#: ``__dict__`` not in this set came from the caller's ``extra={...}`` (the
#: discrimination ``structlog.stdlib.ExtraAdder`` makes).  Built from a throwaway
#: record so it tracks the running stdlib (e.g. 3.12 ``taskName``).
_STD_LOGRECORD_KEYS: frozenset[str] = frozenset(
    _stdlib_logging.LogRecord("", 0, "", 0, "", (), None).__dict__
)

#: Keys the chain owns and a caller's ``extra=`` must never set: schema machinery
#: (re-derived downstream) + OTEL IDs.  Contextvar/service identity (``service.*``
#: / ``execution_id`` / ``agent_id`` / …) is deliberately absent — protected by
#: the "already-present wins" rule instead, so an ``extra`` may *fill* it (no
#: contextvar bound) but never *overwrite* a bound one.
_EXTRA_RESERVED_KEYS: frozenset[str] = frozenset(
    {"event", "message", "level", "timestamp", "schema_version", "trace_id", "span_id"}
)

#: Logger-name roots whose records are *ours*.  Covers every import spelling:
#: ``agents.*`` (``PYTHONPATH=.``), ``persatrix_agents.*`` (editable install),
#: and the explicit ``Persatrix.*`` server / shipper loggers.  Anything else
#: (``grpc`` / ``asyncio`` / ``anthropic`` / …) is third-party and is left
#: untouched — see the module docstring for why that matters.
_APP_LOG_ROOTS: frozenset[str] = frozenset({"agents", "persatrix_agents", "Persatrix"})


def surface_stdlib_extra(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor: lift our own foreign record's ``extra=`` keys into
    the event dict, without clobbering a chain-owned field or touching
    third-party records.

    ``ProcessorFormatter`` attaches the originating record under ``_record``; on
    the structlog-native path there is none, so this is a no-op there (exactly
    where the ``extra=`` audits do not live).
    """
    record = event_dict.get("_record")
    if record is None:
        return event_dict
    # Scope to our own records — never surface a third-party foreign record's
    # attributes (see module docstring: out of scope + the shipper feedback-loop
    # CI hang).
    name = getattr(record, "name", "") or ""
    if name.split(".", 1)[0] not in _APP_LOG_ROOTS:
        return event_dict
    for key, value in record.__dict__.items():
        if (
            key not in _STD_LOGRECORD_KEYS
            and key not in _EXTRA_RESERVED_KEYS
            and key not in event_dict
        ):
            event_dict[key] = value
    return event_dict
