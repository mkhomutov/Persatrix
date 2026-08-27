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
from agents.persona_runtime.close_path import persist_fanned_closes

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
