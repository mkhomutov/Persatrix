"""RFC 0052 §E — the convener-side reverser for a standing convene timer id
(v0.3.11 PR 7c-ii-a). TDD-first: pins the Python inverse of the Go producer's
timer-id encoding so a fired ``ScheduledWake(callback_kind="convene")`` recovers
exactly the group channel to convene from its ``timer_id`` alone (the wake
carries no ``channel_id`` — ``agents/event_loop_types.py`` ``ScheduledWake``).

This is the consumer half of the id contract PR 7c-i's ``standing_schedule.go``
producer established; these cases are the behavioural mirror of
``internal/channels/standing_schedule_test.go`` ``TestStandingConveneTimerID_RoundTrips``.
A cross-language drift guard pins the two Go source constants this port copies
(the channel-name charset + the ``convene-`` prefix / ``convene`` kind) so a
Go-side change to either breaks loudly here rather than silently desyncing the
reverser from the encoder it must invert.
"""

from __future__ import annotations

import re
from pathlib import Path

from agents.convene_timer import (
    STANDING_CONVENE_KIND,
    parse_standing_convene_timer_id,
)

# CWD-relative repo paths (CI runs pytest from the repo root), mirroring the
# sibling test_cross_language_convene_wire_drift.py.
_CHANNELS_GO = Path("internal/channels/channels.go")
_STANDING_SCHEDULE_GO = Path("internal/channels/standing_schedule.go")


# ─── Round-trip / recovery contract ────────────────────────────────────────────


class TestParseStandingConveneTimerID:
    """The reverser recovers the group channel a convene timer id encodes, and
    rejects every id that is not one this producer emits."""

    def test_recovers_group_channel_from_convene_timer_id(self) -> None:
        # The encoder is ``standingConveneTimerID("group:<name>") = "convene-<name>"``
        # (standing_schedule.go); parse is its exact inverse over every group name
        # the channel-name charset admits.
        cases = {
            "convene-planning": "group:planning",
            "convene-foo": "group:foo",
            "convene-ab": "group:ab",
            "convene-x9": "group:x9",
            "convene-weekly-arch-review": "group:weekly-arch-review",
            "convene-a-b-c-2": "group:a-b-c-2",
            # A channel literally named ``convene-foo`` encodes to
            # ``convene-convene-foo`` and must decode back — the prefix strip is
            # once, not greedy (round-trips ``group:convene-foo`` in the Go test).
            "convene-convene-foo": "group:convene-foo",
        }
        for timer_id, want in cases.items():
            assert parse_standing_convene_timer_id(timer_id) == want, timer_id

    def test_rejects_non_convene_timer_ids(self) -> None:
        # Mirrors the Go reverser's reject set: other kinds' entries, a bare or
        # empty prefix, and ids that are schema-valid ``autonomy.timers[].id``s
        # (the charset admits ``_`` and interior ``-``) but decode to a name no
        # group channel could carry — parse must reject, never hand back an
        # un-addressable ``group:...``.
        for not_convene in [
            "legacy_tick",
            "reflection",
            "memory_consolidation",
            "convene",  # the bareword kind, not the ``convene-`` prefix
            "convene-",  # empty name after the prefix
            "planning",  # no prefix at all
            "convene-foo_bar",  # ``_`` is a valid timer-id char, never a channel name
            "convene--x",  # leading hyphen after the prefix — channel name rejects
            "convene-a",  # single char — the channel-name charset needs >= 2
            "convene-foo-",  # trailing hyphen — channel name rejects
            "convene-Planning",  # uppercase — channel names are lowercase
            # Trailing newline: Python ``$`` matches before it but Go's RE2 does
            # not, so a plain ``re.match`` would over-accept relative to the Go
            # encoder. fullmatch (the reverser uses it) rejects, staying byte-exact.
            "convene-foo\n",
        ]:
            assert parse_standing_convene_timer_id(not_convene) is None, not_convene

    def test_kind_constant_matches_wire_value(self) -> None:
        # The one bareword the dispatch branch (tick.py) compares against.
        assert STANDING_CONVENE_KIND == "convene"


# ─── Cross-language drift guards ────────────────────────────────────────────────


class TestGoDriftGuards:
    """The reverser copies two Go source facts (the channel-name charset and the
    ``convene-`` prefix / ``convene`` kind). Pin both against the Go source so a
    Go-side change desyncs LOUDLY here — the sibling of the wallet reserve's
    Go<->Python drift pin."""

    def test_channel_name_pattern_matches_go(self) -> None:
        src = _CHANNELS_GO.read_text(encoding="utf-8")
        m = re.search(
            r"channelNamePattern\s*=\s*regexp\.MustCompile\(`([^`]+)`\)", src
        )
        assert m, "channelNamePattern literal not found — channels.go shape drifted"
        go_pattern = m.group(1)
        # The reverser's channel-name check must be exactly the Go pattern: a
        # looser Python pattern would decode a timer id the producer never emits
        # into a bogus convene target; a stricter one would drop a legitimate one.
        from agents.convene_timer import _CHANNEL_NAME_PATTERN

        assert _CHANNEL_NAME_PATTERN.pattern == go_pattern, (
            "Python channel-name pattern drifted from Go channelNamePattern"
        )

    def test_prefix_and_kind_match_go(self) -> None:
        src = _STANDING_SCHEDULE_GO.read_text(encoding="utf-8")
        prefix_m = re.search(
            r'standingConveneTimerPrefix\s*=\s*"([^"]+)"', src
        )
        kind_m = re.search(r'StandingConveneKind\s*=\s*"([^"]+)"', src)
        assert prefix_m and kind_m, "standing_schedule.go constant shape drifted"

        from agents.convene_timer import _STANDING_CONVENE_TIMER_PREFIX

        assert _STANDING_CONVENE_TIMER_PREFIX == prefix_m.group(1)
        assert STANDING_CONVENE_KIND == kind_m.group(1)
