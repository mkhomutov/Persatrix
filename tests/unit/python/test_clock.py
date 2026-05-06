"""
Unit tests for :mod:`agents.clock` (RFC 0021 P1 PR 1).

Covers the ``Clock`` Protocol surface, the ``WallClock`` production
implementation, and the ``FrozenClock`` test implementation.  Tests
here are deliberately minimal — exhaustive temporal-rendering coverage
lives in :mod:`tests.unit.python.test_temporal_rendering`; this file
pins only the clock-seam contract that downstream callers rely on.
"""

from __future__ import annotations

import time
from typing import get_type_hints

import pytest

from agents.clock import Clock, FrozenClock, WallClock


class TestWallClock:
    def test_now_returns_float_close_to_time_time(self) -> None:
        clock = WallClock()
        before = time.time()
        observed = clock.now()
        after = time.time()
        assert isinstance(observed, float)
        # WallClock is a thin wrapper; the returned epoch must lie in
        # the bracket recorded around the call.
        assert before <= observed <= after

    def test_now_iso_default_zone_is_utc(self) -> None:
        clock = WallClock()
        iso = clock.now_iso()
        assert isinstance(iso, str)
        # UTC offset renders as ``+00:00`` per ISO-8601; the trailing
        # ``Z`` form is intentionally avoided to keep parsing symmetric
        # with non-UTC zones.
        assert iso.endswith("+00:00")

    def test_now_iso_honors_configured_timezone(self) -> None:
        clock = WallClock(tz="America/Los_Angeles")
        iso = clock.now_iso()
        # PT is either -07:00 (PDT) or -08:00 (PST); both are valid
        # depending on the wall-clock instant.  Pin the negative-offset
        # invariant rather than the exact value.
        assert iso.endswith("-07:00") or iso.endswith("-08:00")

    def test_unknown_timezone_raises_at_construction(self) -> None:
        with pytest.raises(Exception):
            WallClock(tz="Not/A_Real_Zone")


class TestFrozenClock:
    def test_now_returns_pinned_value(self) -> None:
        clock = FrozenClock(at=1_700_000_000.0)
        assert clock.now() == 1_700_000_000.0
        # Idempotent across repeated reads — no implicit advance.
        assert clock.now() == 1_700_000_000.0

    def test_advance_moves_now_forward(self) -> None:
        clock = FrozenClock(at=1_000.0)
        clock.advance(5.0)
        assert clock.now() == 1_005.0
        clock.advance(0.5)
        assert clock.now() == 1_005.5

    def test_advance_negative_raises(self) -> None:
        # The frozen-clock contract is monotonic: tests exercise
        # forward-only advance.  Backward jumps would mask off-by-one
        # boundary errors in the rendering layer, so the API rejects
        # them at the seam rather than at the rendering site.
        clock = FrozenClock(at=1_000.0)
        with pytest.raises(ValueError):
            clock.advance(-1.0)

    def test_set_replaces_current_time(self) -> None:
        clock = FrozenClock(at=1_000.0)
        clock.set(42.0)
        assert clock.now() == 42.0

    def test_now_iso_honors_configured_timezone(self) -> None:
        # Pinned epoch 1714055520 == 2024-04-25T14:32:00+00:00 (PDT in
        # Los_Angeles is UTC-07:00 on this date).
        clock = FrozenClock(at=1_714_055_520.0, tz="America/Los_Angeles")
        assert clock.now_iso() == "2024-04-25T07:32:00-07:00"

    def test_now_iso_default_zone_is_utc(self) -> None:
        clock = FrozenClock(at=1_714_055_520.0)
        assert clock.now_iso() == "2024-04-25T14:32:00+00:00"


class TestClockProtocol:
    def test_wall_clock_satisfies_protocol(self) -> None:
        # Static-shape check: the production type satisfies the
        # Protocol surface.  A failing assertion here surfaces a
        # signature drift between the impls and the seam contract.
        clock: Clock = WallClock()
        assert callable(clock.now)
        assert callable(clock.now_iso)

    def test_frozen_clock_satisfies_protocol(self) -> None:
        clock: Clock = FrozenClock(at=0.0)
        assert callable(clock.now)
        assert callable(clock.now_iso)

    def test_protocol_advertises_now_and_now_iso(self) -> None:
        # The Protocol must declare exactly these two methods.  An
        # accidental rename in the protocol surface (e.g. ``utcnow``)
        # would silently break callers that consume ``Clock`` by
        # duck-typing; pin the names here.
        hints = get_type_hints(Clock.now)
        assert hints.get("return") is float
        hints_iso = get_type_hints(Clock.now_iso)
        assert hints_iso.get("return") is str
