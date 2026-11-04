"""Persatrix log-record redactor surface (RFC 0018 § F).

The default :class:`NoopRedactor` is a pass-through, and logs and spans still
install it.  :class:`SecretRedactor` masks the secrets Go's ``SecretRedactor``
masks, with Go's own patterns (RFC 0028 PR 3a), for the decision log RFC 0028
PR 3 adds.  A PII scrubber is still the responsibility of a future security
RFC under the RFC 0009 umbrella.

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

import re
from typing import Any, Protocol, runtime_checkable

from .redact_patterns import REDACT_PATTERNS


@runtime_checkable
class Redactor(Protocol):
    """Redaction hook called once per log record (and per span-attribute bag)."""

    def redact(self, record: dict[str, Any]) -> dict[str, Any]:
        """Return a (possibly redacted) copy of ``record``.

        Implementations must return a new dict (or the input unchanged) and
        must not raise on well-formed input.  Errors surface as the unredacted
        record being emitted; the structlog chain logs a warning out-of-band,
        and :func:`agents.observability.audit_record.emit_audit_record`
        mirrors that out-of-band warning for the pre-``configure_logging``
        window (PR #340 review S2).

        Idempotence (PR #340 review N1)
        -------------------------------
        Implementations **must** be idempotent — ``redact(redact(r))``
        must equal ``redact(r)``.  Some emission paths run the redactor
        twice on the same record (e.g. RFC 0026 audit emissions call
        :func:`~agents.memory._facts_audit.emit_audit` which redacts
        explicitly *before* handing the payload to the stdlib logger,
        whose ``ProcessorFormatter.foreign_pre_chain`` then runs the
        chain's :func:`_apply_redactor` over the same record).  A
        non-idempotent scrubber that masks differently on re-application
        (e.g. progressive truncation, repeated hashing) would
        double-mask and lose information.  :class:`NoopRedactor` is
        trivially idempotent; future PII / secret scrubbers should
        either short-circuit on a sentinel (e.g. an ``"_redacted": True``
        marker on the dict) or compose to a fixed point.
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


#: How deep :meth:`SecretRedactor.redact` walks, and what it puts past that
#: depth: the number and marker of Go's ``RedactStruct``.  This walk counts
#: one step per level, so it keeps values down to 32 levels; Go counts two per
#: level of a JSON-shaped ``map[string]any`` (the map, then the value inside
#: it) and stops at 16.  A value past the cap, a cycle included, is replaced,
#: never kept.
_MAX_DEPTH = 32
_DEPTH_MARKER = "[REDACTED:max-depth-exceeded]"
_CONTAINERS = (dict, list, tuple)


class SecretRedactor:
    """Masks API keys, tokens and passwords as Go's ``SecretRedactor`` does.

    The patterns are Go's, copied by ``cmd/genpatterns`` into
    :mod:`agents.observability.redact_patterns`, and applied in Go's order;
    a match becomes ``[REDACTED:<pattern name>]``.  A parity test runs Go's
    output for a set of samples against this class.

    Python's ``re`` reads ``\\s``, ``$`` and ``(?i)`` differently from Go's
    RE2 outside ASCII (the split ISSUE-0154 records for the sanitizer), and a
    pattern that took more text here could swallow the start of a secret a
    later pattern masks.  So the generator rewrites each expression to Go's
    meaning: Go's five ASCII spaces for ``\\s``, ``\\Z`` for ``$``, ASCII
    mode, and the two non-ASCII letters Go's ``(?i)`` folds with s and k.  The
    parity samples include text the two engines would otherwise read
    differently.
    """

    def __init__(self) -> None:
        # Each marker's backslashes are doubled, so `sub` reads it as plain
        # text, never as a reference to a matched group.
        self._patterns = tuple(
            (re.compile(expr), f"[REDACTED:{name}]".replace("\\", "\\\\"))
            for name, expr in REDACT_PATTERNS
        )

    def redact_text(self, text: str) -> str:
        """Return *text* with every match of every pattern masked."""
        for pattern, marker in self._patterns:
            text = pattern.sub(marker, text)
        return text

    def redact(self, record: dict[str, Any]) -> dict[str, Any]:
        """Return a copy of *record* with every string in it masked.

        Dicts, lists and tuples are walked; keys are kept as they are, as Go
        keeps map keys; other values are returned unchanged.  A container the
        record holds in two places at the same depth is copied once and held
        in both, as :func:`copy.deepcopy` keeps it.
        """
        # One result per container and depth: a container reached down many
        # paths, or a cycle with two ways back, is walked once per depth
        # rather than once per path, which would double at every level.
        seen: dict[tuple[int, int], Any] = {}
        return {key: self._walk(value, 1, seen) for key, value in record.items()}

    def _walk(self, value: Any, depth: int, seen: dict[tuple[int, int], Any]) -> Any:
        if depth > _MAX_DEPTH:
            return _DEPTH_MARKER
        if isinstance(value, str):
            return self.redact_text(value)
        if not isinstance(value, _CONTAINERS):
            return value
        key = (id(value), depth)
        if key not in seen:
            if isinstance(value, dict):
                seen[key] = {k: self._walk(item, depth + 1, seen) for k, item in value.items()}
            elif isinstance(value, list):
                seen[key] = [self._walk(item, depth + 1, seen) for item in value]
            else:
                seen[key] = tuple(self._walk(item, depth + 1, seen) for item in value)
        return seen[key]


__all__ = ["NoopRedactor", "Redactor", "SecretRedactor"]
