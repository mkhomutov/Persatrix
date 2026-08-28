"""v0.3.15 PR 3 review fix — the room fan's per-record persist guard.

``close_scope`` pops EVERY record it closes from the tracker before the
first persist runs, so an exception escaping one record's persist would
silently discard the rest — closed, gone from the open map, and with no
idle sweep left to find them.  :func:`persist_fanned_closes` mirrors
the per-iteration guard on the idle-flush loops
(``episode_routing._store_event_episode``, PR-3 review #13): one
``try`` per record, a warning naming the failing record's identity,
and the fan keeps persisting the siblings.
"""

from __future__ import annotations

import logging

from agents.memory.boundary_detectors import REASON_STRUCTURAL
from agents.memory.interactions import Interaction, InteractionTracker
from agents.persona_runtime.close_path import (
    fan_close_scope,
    persist_fanned_closes,
)

_SCOPE = "group:planning"


def _fanned_records(n: int) -> list[Interaction]:
    """``n`` records in one scope, closed by the fan — all already
    popped from the tracker, exactly the state the guard exists for."""
    tracker = InteractionTracker()
    for i in range(n):
        tracker.add_turn(_SCOPE, speaker_id=f"speaker-{i}")
    return tracker.close_scope(_SCOPE, reason=REASON_STRUCTURAL)


async def test_all_records_persist_in_fan_order():
    records = _fanned_records(3)
    persisted: list[Interaction] = []

    async def persist(interaction: Interaction) -> None:
        persisted.append(interaction)

    await persist_fanned_closes(records, persist)
    assert persisted == records


async def test_one_failing_persist_does_not_discard_the_rest(caplog):
    records = _fanned_records(3)
    persisted: list[Interaction] = []

    async def persist(interaction: Interaction) -> None:
        if interaction is records[0]:
            raise RuntimeError("phase-1 insert blew up")
        persisted.append(interaction)

    with caplog.at_level(logging.WARNING):
        await persist_fanned_closes(records, persist)

    assert persisted == records[1:], (
        "records closed by the fan are already gone from the open map; "
        "one failure must not discard the siblings"
    )
    assert any(
        records[0].interaction_id in message for message in caplog.messages
    ), "the failure is logged with the failing record's identity"


async def test_fan_designates_one_conversation_lead():
    """PR #846: one room close is ONE conversation ending — exactly one
    record carries the finalize's conversation-level effects (the §H
    reflect tick, the DM relationship bump)."""
    records = _fanned_records(3)

    async def persist(interaction: Interaction) -> None:
        pass

    designated = await persist_fanned_closes(records, persist)
    assert designated is True
    assert [r.conversation_lead for r in records] == [True, False, False]


async def test_lead_falls_to_the_next_record_when_the_first_cannot_carry_it():
    """PR #846 re-review: the lead is the first record whose persist
    actually SCHEDULED Phase 2 — a raising persist (a Phase-1 failure)
    or a ``False`` return (``persist_closed_interaction``'s early
    exits) forfeits only that record, never the whole event's effects."""
    records = _fanned_records(3)

    async def persist(interaction: Interaction) -> bool | None:
        if interaction is records[0]:
            raise RuntimeError("phase-1 blew up")
        if interaction is records[1]:
            return False  # early exit — no Phase 2 scheduled
        return True

    designated = await persist_fanned_closes(records, persist)
    assert designated is True
    assert [r.conversation_lead for r in records] == [False, False, True]


async def test_designate_lead_false_marks_every_record_a_follower():
    """PR #846 re-review: a same-event earlier fan (the inline cap
    close) already carries the lead — the follow-up fan must not mint
    a second one."""
    records = _fanned_records(2)

    async def persist(interaction: Interaction) -> None:
        pass

    designated = await persist_fanned_closes(
        records, persist, designate_lead=False,
    )
    assert designated is False
    assert [r.conversation_lead for r in records] == [False, False]


async def test_fan_close_scope_owns_the_room_close():
    """PR #846 re-review: the owned fan skips replay-opened records,
    applies per-record wire admission, stamps ONE instant, and persists
    behind the guard."""
    ticks = iter(range(1_000, 2_000))
    tracker = InteractionTracker(clock=lambda: float(next(ticks)))
    admitted = tracker.add_turn(_SCOPE, speaker_id="iron-fox")
    blank = tracker.add_turn(_SCOPE, speaker_id="nova-sparrow")
    admitted.wire_interaction_id = "wire-A"
    foreign = tracker.add_turn(_SCOPE, speaker_id="ember-owl")
    foreign.wire_interaction_id = "wire-B"
    replayed = tracker.add_turn(
        _SCOPE, speaker_id="quartz-heron", replayed=True,
    )
    persisted: list[Interaction] = []

    async def persist(interaction: Interaction) -> None:
        persisted.append(interaction)

    closed = await fan_close_scope(
        tracker, _SCOPE, reason=REASON_STRUCTURAL, persist=persist,
        wire_anchor="wire-A",
    )

    assert closed == persisted == [admitted, blank], (
        "wire-B's record and the replay-opened record must survive"
    )
    assert foreign.is_open and replayed.is_open
    assert len({r.closed_at for r in closed}) == 1, "one instant"
    assert [r.conversation_lead for r in closed] == [True, False]


async def test_fan_close_scope_strict_blank_anchor():
    """The vote-discharge posture: a blank anchor admits only
    blank-stamped records — a positively-identified record is never
    buried by an unanchored close."""
    tracker = InteractionTracker()
    blank = tracker.add_turn(_SCOPE, speaker_id="iron-fox")
    stamped = tracker.add_turn(_SCOPE, speaker_id="ember-owl")
    stamped.wire_interaction_id = "wire-B"

    async def persist(interaction: Interaction) -> None:
        pass

    closed = await fan_close_scope(
        tracker, _SCOPE, reason=REASON_STRUCTURAL, persist=persist,
        wire_anchor="", blank_anchor_admits=False,
    )
    assert closed == [blank]
    assert stamped.is_open
