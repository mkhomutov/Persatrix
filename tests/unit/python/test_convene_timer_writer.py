"""RFC 0052 §E — the agents.yaml convene-timer WRITER (v0.3.11 PR 7c-ii-b).

TDD-first. Pins the two correctness contracts the Go producer's
``standing_schedule.go`` deferred to the consumer (its lines 49-62), each of
which the naive "just append a ``timers`` entry" gets wrong:

  * **Level bump** — ``server_persona.initialize_persona_agents`` only builds the
    ``TickScheduler`` (the ``EventLoop`` a timer arms on) when ``autonomy.level``
    is ``semi-autonomous`` / ``autonomous``; a ``reactive`` convener silently
    ignores a ``timers`` entry. The writer must raise a below-scheduler level to
    ``semi-autonomous`` and must never downgrade an already-scheduling one.

  * **Tick carry-forward** — ``server_persona`` passes
    ``register_legacy_timer=(timers is None)``, so a convener that *today* ticks
    on ``tick_interval_seconds`` with **no** ``timers`` block loses that heartbeat
    the instant a ``timers`` block appears. The writer must materialize that
    implicit legacy tick as an explicit ``{id: legacy_tick, kind: tick}`` entry —
    but ONLY when the convener was already scheduling with no ``timers`` block; a
    just-bumped ``reactive`` convener had no tick to carry (so gaining a schedule
    must not silently start ordinary autonomy spend), and a convener already on
    the ``timers`` path keeps its explicit set verbatim.

Plus purity (the input block is never mutated), idempotency (a second application
is a no-op — the convene entry refreshes in place, never duplicates), and
deterministic timer-id ordering (a stable config-round-trip diff, matching
``StandingConveneTimers``).
"""

from __future__ import annotations

import copy

import pytest

from agents.convene_timer import STANDING_CONVENE_KIND
from agents.convene_timer_writer import (
    ConveneSpec,
    merge_convene_timers,
)

# The convene timer a standing ``group:planning`` (daily) implies — the common
# fixture across cases below.
_PLANNING = ConveneSpec(channel_id="group:planning", interval_seconds=86400)


def _convene_entry(timer_id: str, interval: int) -> dict:
    return {"id": timer_id, "interval_seconds": interval, "kind": STANDING_CONVENE_KIND}


class TestLevelBump:
    """The scheduler exists only at ``semi-autonomous`` / ``autonomous``; the
    writer raises a below-scheduler level to the minimum that runs a scheduler and
    never touches an already-scheduling one."""

    def test_reactive_convener_is_bumped_to_semi_autonomous(self) -> None:
        out = merge_convene_timers({"level": "reactive"}, [_PLANNING])
        assert out["level"] == "semi-autonomous"

    def test_passive_convener_is_bumped_to_semi_autonomous(self) -> None:
        out = merge_convene_timers({"level": "passive"}, [_PLANNING])
        assert out["level"] == "semi-autonomous"

    def test_missing_level_defaults_reactive_and_is_bumped(self) -> None:
        # ``autonomy.get("level", "reactive")`` is the shipped default in
        # server_persona.py — a convener config with no ``level`` is reactive and
        # must be bumped, or its timer never arms.
        out = merge_convene_timers({}, [_PLANNING])
        assert out["level"] == "semi-autonomous"

    def test_semi_autonomous_is_preserved(self) -> None:
        out = merge_convene_timers({"level": "semi-autonomous"}, [_PLANNING])
        assert out["level"] == "semi-autonomous"

    def test_autonomous_is_not_downgraded(self) -> None:
        out = merge_convene_timers({"level": "autonomous"}, [_PLANNING])
        assert out["level"] == "autonomous"


class TestTickCarryForward:
    """Adding a ``timers`` block flips ``register_legacy_timer`` off — the writer
    must carry an active legacy tick forward as an explicit entry, and only then."""

    def test_scheduling_convener_with_legacy_tick_carries_it_forward(self) -> None:
        # semi-autonomous + no ``timers`` block => the legacy tick fires today at
        # tick_interval_seconds. Writing a ``timers`` block would drop it, so it
        # must appear as an explicit {kind: tick} entry at the SAME interval.
        out = merge_convene_timers(
            {"level": "semi-autonomous", "tick_interval_seconds": 30}, [_PLANNING]
        )
        tick = [t for t in out["timers"] if t["kind"] == "tick"]
        assert tick == [{"id": "legacy_tick", "interval_seconds": 30, "kind": "tick"}]

    def test_carry_forward_uses_the_default_interval_when_unset(self) -> None:
        # No explicit tick_interval_seconds: the legacy tick still fires (default
        # 60 in server_persona.py), so the carried entry must be 60, not dropped.
        out = merge_convene_timers({"level": "semi-autonomous"}, [_PLANNING])
        tick = [t for t in out["timers"] if t["kind"] == "tick"]
        assert tick == [{"id": "legacy_tick", "interval_seconds": 60, "kind": "tick"}]

    def test_bumped_reactive_convener_gets_no_legacy_tick(self) -> None:
        # A reactive convener had NO tick (reactive never enters the scheduler
        # branch). After the bump it must fire ONLY the convene timer — gaining a
        # schedule must not silently start ordinary autonomy LLM spend.
        out = merge_convene_timers({"level": "reactive"}, [_PLANNING])
        assert all(t["kind"] != "tick" for t in out["timers"])
        assert out["timers"] == [_convene_entry("convene-planning", 86400)]

    def test_existing_timers_block_is_preserved_without_a_legacy_tick(self) -> None:
        # Already on the timers path (register_legacy_timer already False): the
        # explicit set is kept verbatim and NO legacy_tick is synthesized.
        existing = {
            "level": "semi-autonomous",
            "tick_interval_seconds": 30,  # dead once timers is present
            "timers": [
                {"id": "reflection", "interval_seconds": 3600, "kind": "reflection"}
            ],
        }
        out = merge_convene_timers(existing, [_PLANNING])
        assert {"id": "reflection", "interval_seconds": 3600, "kind": "reflection"} in out[
            "timers"
        ]
        assert all(t["id"] != "legacy_tick" for t in out["timers"])

    def test_empty_timers_list_is_the_timers_path_not_the_legacy_path(self) -> None:
        # ``timers: []`` is present-but-empty (the v0.3.3 stock default). It is the
        # timers path — register_legacy_timer is already False — so no heartbeat
        # exists to carry, and the writer must not conjure one.
        out = merge_convene_timers(
            {"level": "semi-autonomous", "timers": []}, [_PLANNING]
        )
        assert out["timers"] == [_convene_entry("convene-planning", 86400)]


class TestConveneEntry:
    """The convene entry itself — schema-valid id, kind, interval — and multiples."""

    def test_convene_entry_is_added_with_the_encoded_id_and_kind(self) -> None:
        out = merge_convene_timers({"level": "reactive"}, [_PLANNING])
        assert _convene_entry("convene-planning", 86400) in out["timers"]

    def test_multiple_standing_channels_yield_multiple_entries_sorted(self) -> None:
        # ``timers: []`` isolates the multi-convene ordering from the tick
        # carry-forward (an already-timers convener has no legacy heartbeat).
        specs = [
            ConveneSpec(channel_id="group:planning", interval_seconds=86400),
            ConveneSpec(channel_id="group:arch-review", interval_seconds=604800),
        ]
        out = merge_convene_timers({"level": "autonomous", "timers": []}, specs)
        assert out["timers"] == [
            _convene_entry("convene-arch-review", 604800),
            _convene_entry("convene-planning", 86400),
        ]

    def test_non_group_channel_is_rejected(self) -> None:
        # Standing channels are group-only (validateAutonomousChannelType); a
        # non-group id cannot encode a timer id, so passing one is a caller bug —
        # fail loud rather than emit a malformed / un-addressable entry.
        with pytest.raises(ValueError):
            merge_convene_timers(
                {"level": "reactive"},
                [ConveneSpec(channel_id="dm:alice", interval_seconds=3600)],
            )


class TestPurityAndIdempotency:
    def test_input_block_is_not_mutated(self) -> None:
        original = {
            "level": "reactive",
            "tick_interval_seconds": 30,
            "timers": [{"id": "reflection", "interval_seconds": 3600, "kind": "reflection"}],
        }
        snapshot = copy.deepcopy(original)
        merge_convene_timers(original, [_PLANNING])
        assert original == snapshot

    def test_applying_twice_is_a_no_op(self) -> None:
        # The config round-trip must converge: re-deriving from the writer's own
        # output changes nothing (the tick carry-forward is gated on "no timers
        # block", which the first application always establishes).
        once = merge_convene_timers(
            {"level": "semi-autonomous", "tick_interval_seconds": 30}, [_PLANNING]
        )
        twice = merge_convene_timers(once, [_PLANNING])
        assert twice == once

    def test_existing_convene_entry_interval_is_refreshed_in_place(self) -> None:
        # An operator who shortens the schedule re-runs the writer; the convene
        # entry updates in place rather than duplicating (a duplicate id would
        # doubly-arm the same channel).
        stale = merge_convene_timers(
            {"level": "reactive"}, [ConveneSpec("group:planning", 86400)]
        )
        refreshed = merge_convene_timers(stale, [ConveneSpec("group:planning", 3600)])
        convene = [t for t in refreshed["timers"] if t["kind"] == STANDING_CONVENE_KIND]
        assert convene == [_convene_entry("convene-planning", 3600)]

    def test_other_autonomy_knobs_pass_through_untouched(self) -> None:
        out = merge_convene_timers(
            {
                "level": "semi-autonomous",
                "max_actions_per_tick": 5,
                "idle_after_ticks": 20,
                "salience_threshold": 0.8,
            },
            [_PLANNING],
        )
        assert out["max_actions_per_tick"] == 5
        assert out["idle_after_ticks"] == 20
        assert out["salience_threshold"] == 0.8


class TestLegacyTickConstantsMatchTickModule:
    """Cross-module drift pin: the carried-forward tick reuses the id/kind the
    shipped ``TickScheduler`` registers, so the materialized entry is identical to
    the wake the persona would otherwise have fired (and shares its cache row)."""

    def test_legacy_tick_id_and_kind_mirror_tick_module(self) -> None:
        from agents.convene_timer_writer import _LEGACY_TICK_ID, _LEGACY_TICK_KIND
        from agents.tick import _LEGACY_TIMER_ID

        assert _LEGACY_TICK_ID == _LEGACY_TIMER_ID
        # The kind the shipped scheduler stamps on the legacy wake.
        assert _LEGACY_TICK_KIND == "tick"
