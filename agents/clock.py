"""
Clock seam for the temporal layer (RFC 0021 §B Phase 1).

The temporal layer — now-anchor in the system prompt, recency rendering
on recall, future commitments — must read wall-clock time through a
single, mockable seam.  Without it, every test that asserts "rendered
as '3 days ago'" becomes flaky against the real wall clock.

This module provides:

* :class:`Clock` — Protocol with ``now()`` and ``now_iso()`` methods.
* :class:`WallClock` — production implementation; thin wrapper around
  :func:`time.time` that renders the configured timezone for ISO-8601.
* :class:`FrozenClock` — test implementation with ``advance(seconds)``
  and ``set(epoch)`` for deterministic boundary testing.

RFC 0020 PR 3 introduced a sibling ``Clock`` Protocol in
:mod:`agents.memory.interactions` to inject ``time.time``-shaped
callables into the tracker.  That seam is intentionally narrower —
``() -> float`` — and predates this RFC.  The RFC 0021 P2 follow-up
will alias the tracker's seam to ``Clock.now`` here.  Until then, the
two coexist; this module's :class:`Clock` is the canonical surface
referenced by all new code.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo

DEFAULT_TIMEZONE: str = "UTC"


class Clock(Protocol):
    """Wall-clock seam for the temporal layer.

    Production code reads the current time through ``now()`` (epoch
    seconds, UTC).  Operator-facing prompts read ``now_iso()`` for an
    ISO-8601 string rendered in the agent's configured timezone.
    """

    def now(self) -> float:
        """Seconds since epoch (UTC)."""
        ...

    def now_iso(self) -> str:
        """ISO-8601 representation of :meth:`now` in the configured zone."""
        ...


def _format_iso(epoch: float, tz: ZoneInfo) -> str:
    # Truncate to whole seconds to keep the rendered string operator-
    # friendly; sub-second precision in a system-prompt timestamp is
    # noise that costs tokens without helping the LLM.
    return datetime.fromtimestamp(int(epoch), tz=tz).isoformat()


class WallClock:
    """Production clock — reads :func:`time.time` and renders in ``tz``."""

    def __init__(self, tz: str | None = None) -> None:
        # ``ZoneInfo`` raises on unknown zones; surface the bad config
        # at construction time so an operator misconfiguration cannot
        # masquerade as a runtime "now() returned None" later.
        self._tz: ZoneInfo = ZoneInfo(tz or DEFAULT_TIMEZONE)

    def now(self) -> float:
        return time.time()

    def now_iso(self) -> str:
        return _format_iso(self.now(), self._tz)


class FrozenClock:
    """Deterministic clock for tests.

    Holds a pinned epoch and exposes ``advance(seconds)`` / ``set(epoch)``
    for boundary tests.  Backward jumps via ``advance`` are rejected so
    a test cannot accidentally mask an off-by-one in the rendering
    layer by reversing the clock.
    """

    def __init__(self, at: float, tz: str | None = None) -> None:
        self._t: float = float(at)
        self._tz: ZoneInfo = ZoneInfo(tz or DEFAULT_TIMEZONE)

    def now(self) -> float:
        return self._t

    def now_iso(self) -> str:
        return _format_iso(self._t, self._tz)

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("FrozenClock.advance requires non-negative seconds")
        self._t += float(seconds)

    def set(self, at: float) -> None:
        self._t = float(at)


def resolve_persona_clock(
    config: dict, clock: Clock | None = None,
) -> tuple[Clock, str]:
    """Build the persona's :class:`Clock` and rendered timezone string.

    Reads ``config["persona"]["timezone"]``, defaulting to
    :data:`DEFAULT_TIMEZONE` when absent or blank.  When ``clock`` is
    provided (typically a :class:`FrozenClock` from tests) it is returned
    verbatim; otherwise a :class:`WallClock` against the resolved zone.

    Returning the rendered timezone alongside the clock lets callers thread
    the same string into recency-rendering helpers without re-parsing the
    config — keeping the seam initialization a single call from the
    persona-runtime constructor.
    """
    tz = ((config.get("persona") or {}).get("timezone") or "").strip() or DEFAULT_TIMEZONE
    return clock if clock is not None else WallClock(tz), tz


__all__ = [
    "DEFAULT_TIMEZONE",
    "Clock",
    "FrozenClock",
    "WallClock",
    "resolve_persona_clock",
]
