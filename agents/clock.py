"""
Clock seam for the temporal layer (RFC 0021 §B Phase 1).

The temporal layer — now-anchor in the system prompt, recency rendering
on recall, future commitments — must read wall-clock time through a
single, mockable seam.  Without it, every test that asserts "rendered
as '3 days ago'" becomes flaky against the real wall clock.

This module provides:

* :class:`Clock` — Protocol with ``now()`` and ``now_iso()`` methods.
* :class:`AgentClock` — production implementation; reads agent time
  (below) and renders the configured timezone for ISO-8601.
* :class:`WallClock` — thin wrapper around :func:`time.time`, for callers
  that need real time whatever the agent's clock says.
* :class:`FrozenClock` — test implementation with ``advance(seconds)``
  and ``set(epoch)`` for deterministic boundary testing.

**Agent time.** An agent process normally lives on real time. Setting
``PERSATRIX_CLOCK_START`` to an ISO-8601 instant with a zone shifts the
whole agent: its time starts at that instant and moves with the real clock.
EXP-001 uses it to put each adviser on the meeting's story date.
``PERSATRIX_CLOCK_ANCHOR``, also an instant with a zone, names the real
moment the start belongs to; without it that moment is the process's first
clock read. A restart must pass the same anchor, or its clock would begin
at the start again, earlier than what the last run wrote. Memory
stamps, recency and new events all read :func:`agent_now`; a timestamp the
orchestrator wrote is moved into agent time with :func:`to_agent_time`.
Timers, deadlines, telemetry and anything sent back to the orchestrator
stay on real time.

RFC 0020 PR 3 introduced a sibling ``Clock`` Protocol in
:mod:`agents.memory.interactions` to inject ``time.time``-shaped
callables into the tracker.  That seam is intentionally narrower —
``() -> float`` — and predates this RFC.  The RFC 0021 P2 follow-up
will alias the tracker's seam to ``Clock.now`` here.  Until then, the
two coexist; this module's :class:`Clock` is the canonical surface
referenced by all new code.
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo

DEFAULT_TIMEZONE: str = "UTC"
CLOCK_START_ENV: str = "PERSATRIX_CLOCK_START"
CLOCK_ANCHOR_ENV: str = "PERSATRIX_CLOCK_ANCHOR"

# Seconds added to real time, and the real instant agent time began at, read
# from the settings on first use and then fixed for the life of the process;
# None until then. The anchor stays None on an unshifted clock.
_agent_offset: float | None = None
_agent_anchor: float | None = None


def agent_clock_offset() -> float:
    """How far agent time runs ahead of real time, in seconds (0 unshifted)."""
    global _agent_offset, _agent_anchor
    if _agent_offset is None:
        _agent_offset, _agent_anchor = _read_clock_settings()
    return _agent_offset


def _read_clock_settings() -> tuple[float, float | None]:
    start = _read_instant(CLOCK_START_ENV)
    anchor = _read_instant(CLOCK_ANCHOR_ENV)
    if start is None:
        if anchor is not None:
            raise ValueError(f"{CLOCK_ANCHOR_ENV} is set but {CLOCK_START_ENV} is not")
        return 0.0, None
    if anchor is None:
        anchor = time.time()
    return start - anchor, anchor


def _read_instant(env: str) -> float | None:
    raw = os.environ.get(env, "").strip()
    if not raw:
        return None
    try:
        instant = datetime.fromisoformat(raw)
    except ValueError:
        instant = None
    if instant is None or instant.tzinfo is None:
        raise ValueError(
            f"{env} must be an ISO-8601 instant with a zone, "
            f"such as 2036-10-06T10:00:00+00:00; got {raw!r}"
        )
    return instant.timestamp()


def predates_agent_clock(wall: float) -> bool:
    """Whether a real-time timestamp comes from before this clock began.

    Such a timestamp belongs to an earlier run whose offset is unknown, so
    :func:`to_agent_time` cannot place it. Always False on real time.
    """
    agent_clock_offset()
    return _agent_anchor is not None and wall < _agent_anchor


def agent_now() -> float:
    """The agent's current time, in epoch seconds."""
    return time.time() + agent_clock_offset()


def to_agent_time(wall: float) -> float:
    """Move a real-time timestamp, such as the orchestrator's, into agent time."""
    return wall + agent_clock_offset()


def reset_agent_clock() -> None:
    """Forget the offset, so the next read takes the setting again (tests)."""
    global _agent_offset, _agent_anchor
    _agent_offset = None
    _agent_anchor = None


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


class AgentClock:
    """The persona's clock in production: agent time, rendered in ``tz``."""

    def __init__(self, tz: str | None = None) -> None:
        self._tz: ZoneInfo = ZoneInfo(tz or DEFAULT_TIMEZONE)

    def now(self) -> float:
        return agent_now()

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
        """Replace the pinned epoch — for explicit re-anchoring only.

        Use ``advance`` for forward stepping within a single test phase;
        the monotonicity guard there is what protects rendering-layer
        boundary tests from being silently masked by a clock rewind.
        ``set`` is intentionally unguarded so a fresh test phase can
        start from a different epoch (e.g. resetting around a DST
        transition or to align with a fixture instant); using ``set``
        mid-phase to walk time backward bypasses the same guarantee.
        Construct a new ``FrozenClock`` for an unrelated test phase
        rather than rewinding an existing instance.

        Why not symmetrize with ``advance`` and reject ``at < self._t``?
        Because ``set`` is the re-anchor primitive: phase changes, DST
        resets, and fixture rebuilds all need to move backward in time,
        and forcing those callers to construct a fresh ``FrozenClock``
        would be ergonomic noise without preventing the class of bug
        ``advance`` actually guards against — a monotonic test sequence
        accidentally rewinding mid-flow. The two methods are
        deliberately asymmetric. ISSUE-0045.
        """
        self._t = float(at)


def resolve_persona_clock(
    config: dict, clock: Clock | None = None,
) -> tuple[Clock, str]:
    """Build the persona's :class:`Clock` and rendered timezone string.

    Reads ``config["persona"]["timezone"]``, defaulting to
    :data:`DEFAULT_TIMEZONE` when absent or blank.  When ``clock`` is
    provided (typically a :class:`FrozenClock` from tests) it is returned
    verbatim; otherwise an :class:`AgentClock` against the resolved zone, so
    the prompt's time and the memory stamps it is compared with agree.

    Returning the rendered timezone alongside the clock lets callers thread
    the same string into recency-rendering helpers without re-parsing the
    config — keeping the seam initialization a single call from the
    persona-runtime constructor.
    """
    tz = ((config.get("persona") or {}).get("timezone") or "").strip() or DEFAULT_TIMEZONE
    return clock if clock is not None else AgentClock(tz), tz


__all__ = [
    "CLOCK_ANCHOR_ENV",
    "CLOCK_START_ENV",
    "DEFAULT_TIMEZONE",
    "AgentClock",
    "Clock",
    "FrozenClock",
    "WallClock",
    "agent_clock_offset",
    "agent_now",
    "predates_agent_clock",
    "reset_agent_clock",
    "resolve_persona_clock",
    "to_agent_time",
]
