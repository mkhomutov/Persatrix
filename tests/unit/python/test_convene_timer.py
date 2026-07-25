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
    standing_convene_timer_id,
)

# CWD-relative repo paths (CI runs pytest from the repo root), mirroring the
# sibling test_cross_language_convene_wire_drift.py. The id-grammar
# declarations (channelNamePattern et al.) moved from channels.go to
# identifiers.go in RFC 0037 PR 2's 500-line-cap carve — the parse rule
# follows the declaration, per this pin's own contract.
_CHANNELS_GO = Path("internal/channels/identifiers.go")
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


class TestStandingConveneTimerID:
    """The FORWARD encoder (PR 7c-ii-b): the Python mirror of the Go
    ``standingConveneTimerID`` the writer needs to author an ``autonomy.timers``
    entry from a channel id — and a true inverse of the reverser above."""

    def test_encodes_group_channel_to_convene_timer_id(self) -> None:
        assert standing_convene_timer_id("group:planning") == "convene-planning"
        assert standing_convene_timer_id("group:weekly-arch-review") == (
            "convene-weekly-arch-review"
        )
        # A channel literally named ``convene-foo`` encodes to
        # ``convene-convene-foo`` (the mirror of the greedy-strip reverser case).
        assert standing_convene_timer_id("group:convene-foo") == "convene-convene-foo"

    def test_round_trips_with_the_reverser_both_directions(self) -> None:
        for channel_id in [
            "group:planning",
            "group:ab",
            "group:x9",
            "group:a-b-c-2",
            "group:convene-foo",
        ]:
            timer_id = standing_convene_timer_id(channel_id)
            assert timer_id is not None
            assert parse_standing_convene_timer_id(timer_id) == channel_id
        # Distinct names from the encode-first loop above: reusing ``channel_id``
        # here would rebind a ``str`` loop target to the ``str | None`` the parser
        # returns, which ``mypy tests/`` rejects. The decode-first direction reads
        # better as encoded → decoded anyway.
        for encoded in ["convene-planning", "convene-ab", "convene-convene-foo"]:
            decoded = parse_standing_convene_timer_id(encoded)
            assert decoded is not None
            assert standing_convene_timer_id(decoded) == encoded

    def test_rejects_non_group_or_invalid_names(self) -> None:
        # Standing channels are group-only; a DM/thread id carries a ``:`` the
        # timer-id pattern forbids and is never armed. An out-of-charset name
        # (uppercase, single char, edge hyphen) is not a channel a group could
        # carry — the encoder must reject rather than emit an id that would not
        # reverse.
        for bad in [
            "dm:alice",
            "thread:planning:42",
            "planning",  # no ``group:`` prefix
            "group:",  # empty name
            "group:Planning",  # uppercase
            "group:a",  # single char — needs >= 2
            "group:-x",  # leading hyphen
            "group:x-",  # trailing hyphen
            "group:foo_bar",  # underscore is not a channel-name char
        ]:
            assert standing_convene_timer_id(bad) is None, bad


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
