"""Persatrix log-record redactor surface (RFC 0018 § F).

This module ships the *hook surface only*.  The default :class:`NoopRedactor`
is a pass-through; a real PII / secret scrubber is the responsibility of a
future security RFC under the RFC 0009 umbrella.

The :class:`Redactor` Protocol is the **single** redaction contract shared
across both observability signals:

* RFC 0018 — log records (this module + the structlog chain in
  :mod:`agents.observability.logging`).
* RFC 0019 Phase 2 — opt-in tool-payload capture as span attributes.

Both call sites pass a ``dict[str, Any]`` (the structured event for logs, the
attribute bag for spans) and expect a ``dict[str, Any]`` back.  Implementations
**must not mutate** the input dict in place — the redactor is invoked on every
record / attribute bag and the caller assumes the input is unchanged when the
redactor decides to no-op.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Redactor(Protocol):
    """Redaction hook called once per log record (and per span-attribute bag)."""

    def redact(self, record: dict[str, Any]) -> dict[str, Any]:
        """Return a (possibly redacted) copy of ``record``.

        Implementations must return a new dict (or the input unchanged) and
        must not raise on well-formed input.  Errors surface as the unredacted
        record being emitted; the structlog chain logs a warning out-of-band.
        """
        ...


class NoopRedactor:
    """Default redactor — returns the record unchanged.

    Used until a security RFC ships a real implementation.  The chain still
    invokes :meth:`redact` on every record so future implementations can rely
    on a single hook.
    """

    def redact(self, record: dict[str, Any]) -> dict[str, Any]:
        return record


__all__ = ["NoopRedactor", "Redactor"]
