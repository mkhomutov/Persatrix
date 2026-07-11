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

Both contracts above are *conditional*, and the condition is the sharp edge: the
bump and the ``timers`` block are written ONLY when a convene timer is actually
armed. The natural driver walks every persona and passes
``specs_by_convener.get(persona_id, [])``, so an unguarded writer would bump the
whole fleet to ``semi-autonomous`` and hand each persona an empty ``timers`` block —
:class:`TestNothingToArmIsANoOp` pins the guard.

Behaviour only. The writer's agreement with the sources it mirrors — the agent
schema, ``server_persona``'s gate and defaults, ``tick``'s legacy-timer constants —
is pinned next door in ``test_convene_timer_writer_pins.py``.
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

    def test_supervisor_convener_is_bumped_to_semi_autonomous(self) -> None:
        # ``supervisor`` is a schema-legal level that READS as more autonomous than
        # semi-autonomous but is NOT in server_persona's scheduler gate — so it runs
        # no scheduler and swallows a timers entry exactly as reactive does. The
        # writer's rule is membership in the gate, not a ladder position, so this is
        # a bump, not a downgrade. Named in the module docstring; pinned here.
        out = merge_convene_timers({"level": "supervisor"}, [_PLANNING])
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

    def test_present_but_none_timers_is_the_legacy_path_not_the_timers_path(
        self,
    ) -> None:
        # ``timers:`` with no value (present-but-``None``) is NOT the timers path:
        # server_persona does ``register_legacy_timer = autonomy.get("timers") is
        # None``, so a ``None`` value keeps the legacy tick LIVE (identical to a
        # wholly absent key) and skips ``init_persona_timers`` entirely. A
        # scheduling convener with ``timers: None`` therefore still has a heartbeat
        # to carry — the writer must materialize it, or the tick silently dies the
        # instant a real ``timers`` block is written. Guards the ``is not None``
        # distinction against a ``"timers" in src`` regression, which would wrongly
        # treat this as the timers path and drop the tick.
        out = merge_convene_timers(
            {"level": "semi-autonomous", "tick_interval_seconds": 30, "timers": None},
            [_PLANNING],
        )
        tick = [t for t in out["timers"] if t["kind"] == "tick"]
        assert tick == [{"id": "legacy_tick", "interval_seconds": 30, "kind": "tick"}]


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

    @pytest.mark.parametrize("interval", [0, -1])
    def test_sub_floor_interval_is_rejected(self, interval: int) -> None:
        # The schema's ``interval_seconds`` minimum is 1.0 and
        # ``EventLoop.register_timer`` RAISES below ``_MIN_INTERVAL`` — which
        # ``init_persona_timers`` re-raises, aborting the convener's init. Writing
        # such an entry defers a caller bug to the convener's next boot, where it
        # reads as a persona that will not start. Symmetric with the non-group
        # rejection above: the writer refuses to emit an entry that cannot arm.
        with pytest.raises(ValueError, match="busy-loop floor"):
            merge_convene_timers(
                {"level": "reactive"},
                [ConveneSpec(channel_id="group:planning", interval_seconds=interval)],
            )

    def test_id_collision_with_a_non_convene_timer_is_rejected(self) -> None:
        # ``parse_standing_convene_timer_id``'s docstring explicitly contemplates an
        # operator-named ``convene-*`` timer of another kind (it is why parse is a
        # strict inverse rather than a prefix strip). Such a timer survives the
        # reconcile filter, and an upsert would replace it — kind and all — with the
        # convene entry, silently destroying operator config. Refuse: an id holds one
        # timer, and the writer does not get to decide it is theirs.
        existing = {
            "level": "autonomous",
            "timers": [
                {"id": "convene-planning", "interval_seconds": 3600, "kind": "reflection"}
            ],
        }
        with pytest.raises(ValueError, match="collides"):
            merge_convene_timers(existing, [_PLANNING])

    def test_a_repeated_spec_refreshes_rather_than_colliding(self) -> None:
        # The collision guard reads the PRE-EXISTING (non-convene) ids only, so a
        # ``specs`` iterable naming the same channel twice still upserts to a single
        # entry at the last interval — it must not trip the collision rejection.
        out = merge_convene_timers(
            {"level": "autonomous", "timers": []},
            [ConveneSpec("group:planning", 86400), ConveneSpec("group:planning", 3600)],
        )
        assert out["timers"] == [_convene_entry("convene-planning", 3600)]


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


class TestReconciliation:
    """The writer is authoritative over the convener's convene-kind timers: its
    caller passes the FULL standing-channel set, so a convene timer whose channel is
    no longer standing (absent from ``specs``) is DROPPED — a stale timer must not
    keep firing a wake into a channel that can only decline it. Non-convene timers
    (the writer does not own their kind) are always preserved."""

    def test_stale_convene_timer_for_a_dropped_channel_is_removed(self) -> None:
        # Two channels armed, then one disarmed: re-running the writer with only the
        # surviving channel drops the other's convene timer rather than leaving it
        # armed (firing a wake every interval into a now-declining channel) forever.
        armed_both = merge_convene_timers(
            {"level": "autonomous", "timers": []},
            [
                ConveneSpec("group:planning", 86400),
                ConveneSpec("group:arch-review", 604800),
            ],
        )
        assert {t["id"] for t in armed_both["timers"]} == {
            "convene-planning",
            "convene-arch-review",
        }
        reconciled = merge_convene_timers(
            armed_both, [ConveneSpec("group:planning", 86400)]
        )
        assert reconciled["timers"] == [_convene_entry("convene-planning", 86400)]

    def test_empty_specs_removes_every_convene_timer(self) -> None:
        # Every standing channel disarmed: no convene timer survives.
        armed = merge_convene_timers({"level": "autonomous", "timers": []}, [_PLANNING])
        reconciled = merge_convene_timers(armed, [])
        assert all(t["kind"] != STANDING_CONVENE_KIND for t in reconciled["timers"])

    def test_reconcile_preserves_non_convene_timers(self) -> None:
        # A stale convene timer is dropped, but a co-resident reflection timer (a
        # kind the writer does not own) is kept verbatim.
        existing = {
            "level": "autonomous",
            "timers": [
                {"id": "reflection", "interval_seconds": 3600, "kind": "reflection"},
                _convene_entry("convene-oldchan", 120),
            ],
        }
        reconciled = merge_convene_timers(existing, [_PLANNING])
        ids = {t["id"] for t in reconciled["timers"]}
        assert "reflection" in ids  # non-convene kept
        assert "convene-oldchan" not in ids  # stale convene dropped
        assert "convene-planning" in ids  # current convene added


class TestNothingToArmIsANoOp:
    """A persona that convenes nothing must come back untouched.

    The writer's caller walks personas and passes
    ``specs_by_convener.get(persona_id, [])``, so the EMPTY-specs call is the common
    one — it lands on every non-convener in the fleet. Both of the writer's side
    effects are capability grants and neither may fire on it: the level bump is what
    BUILDS the scheduler (``server_persona`` gates on it), and a ``timers`` block —
    even ``[]`` — is what turns ``register_legacy_timer`` off."""

    def test_reactive_persona_with_no_specs_is_returned_unchanged(self) -> None:
        # The fleet-wide regression: an unconditional bump makes every persona
        # semi-autonomous, so each builds an EventLoop and logs the three
        # ``COST: … will consume LLM tokens continuously`` warnings for a schedule
        # it does not have.
        block = {"level": "reactive", "tick_interval_seconds": 30}
        assert merge_convene_timers(block, []) == block

    def test_missing_autonomy_block_with_no_specs_gains_nothing(self) -> None:
        assert merge_convene_timers(None, []) == {}

    def test_no_specs_does_not_introduce_an_empty_timers_block(self) -> None:
        # ``timers: []`` is not inert: it flips ``register_legacy_timer`` to False.
        # Writing one here is latent (the persona had no tick to lose while below the
        # scheduler gate), but an operator who later prunes the pointless empty block
        # from a persona this writer ALSO bumped would hand it a tick_interval_seconds
        # LLM heartbeat it never had.
        out = merge_convene_timers({"level": "reactive", "tick_interval_seconds": 30}, [])
        assert "timers" not in out

    def test_scheduling_persona_with_no_specs_keeps_its_implicit_tick(self) -> None:
        # No timers block is introduced, so ``register_legacy_timer`` stays True and
        # the implicit heartbeat survives — there is nothing to carry forward BECAUSE
        # nothing is being written. Materializing an explicit legacy_tick here would
        # be a gratuitous diff that also hands the entry a scheduled_wakes cache row.
        block = {"level": "semi-autonomous", "tick_interval_seconds": 30}
        assert merge_convene_timers(block, []) == block

    def test_no_specs_still_drops_a_stale_convene_timer(self) -> None:
        # The one case where empty specs MUST write: reconciling a convener whose
        # last standing channel was disarmed. The timers block already exists, so
        # dropping the stale entry introduces no new capability.
        armed = merge_convene_timers({"level": "autonomous", "timers": []}, [_PLANNING])
        reconciled = merge_convene_timers(armed, [])
        assert reconciled["timers"] == []
        # The bump is one-way: reconciling to zero channels does not lower the level
        # (the writer cannot know whether an operator raised it for another reason).
        assert reconciled["level"] == "autonomous"

    def test_a_bumped_convener_is_not_lowered_when_its_channel_disarms(self) -> None:
        bumped = merge_convene_timers({"level": "reactive"}, [_PLANNING])
        assert bumped["level"] == "semi-autonomous"
        assert merge_convene_timers(bumped, [])["level"] == "semi-autonomous"

