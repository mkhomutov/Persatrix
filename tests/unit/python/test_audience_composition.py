"""ISSUE-0132 (v0.3.16 PR A2) — how the audience check composes.

Scope lock 3 makes the check a **gate input**, not a pre-filter, so the
three readers of the gate's one decision record all see it: §E must NOT
substitute a projection for an audience withhold (a projection lowers an
entry's classification, which is a different axis from who is in the
room), §G's watch must see the withheld entry, and the §G manifest must
not label it as injected.
"""

from __future__ import annotations

import pytest

from agents.confidentiality_tripwire import tripwire_watch_from_event
from agents.memory.episode_types import Episode
from agents.memory.projections import ENTRY_TIER_EPISODE, replace_entry_projections
from agents.persona_runtime.audience import AUDIENCE_LIVE, AUDIENCE_SHADOW, TurnAudience
from agents.persona_runtime.injection_gate import TurnInjectionGate
from agents.persona_runtime.memory_budget import MemoryBudget
from agents.persona_runtime.projection_branch import apply_episode_projections
from agents.persona_runtime.tripwire_watch import stamp_turn_tripwire_watch
from agents.persona_types import AgentEvent, EventType

pytestmark = pytest.mark.asyncio

DM = "dm:alice:iron-fox"
ROOM = "group:standup"

#: Long enough that ``span_hashes`` has something to fingerprint.
_SUMMARY = (
    "Alice said in the DM that the Zephyr acquisition closes on March 3 "
    "and that it should not go beyond the two of us for now."
)


def _audience(mode: str = AUDIENCE_LIVE) -> TurnAudience:
    return TurnAudience(
        mode=mode, acting_channel_id=ROOM,
        rooms={
            ROOM: frozenset({"alice", "iron-fox", "bob"}),
            DM: frozenset({"alice", "iron-fox"}),
        },
        fetches=1, source_rooms=1,
    )


def _episode(episode_id: str, *, interaction_id: str) -> Episode:
    return Episode(
        id=episode_id, agent_id="test-agent", summary=_SUMMARY, context={},
        outcome=None, importance=0.5, access_count=0, last_accessed_at=None,
        tags=[], created_at=1.0, compressed_at=None, compression_level=0,
        interaction_id=interaction_id,
        protection_level="internal",
        source_channel_id=DM,
    )


def _gate(mode: str = AUDIENCE_LIVE) -> TurnInjectionGate:
    return TurnInjectionGate(
        acting="internal", agent_id="test-agent", audience=_audience(mode),
    )


async def test_an_audience_withhold_is_terminal_under_section_e(memory) -> None:
    """The entry is admissible at ``internal`` — §E would happily serve a
    ``public`` projection.  It must not: abstracting the Zephyr close date
    for Bob still tells Bob that Alice mentioned a Zephyr close date."""
    await replace_entry_projections(
        memory, entry_id="ix-1", entry_tier=ENTRY_TIER_EPISODE,
        projections={"public": "A roadmap decision was made."}, created_at=100.0,
    )
    gate = _gate()
    episode = _episode("ep-1", interaction_id="ix-1")
    admitted = gate.filter_entries("episodic", [episode])
    assert admitted == []

    _, episodic = await apply_episode_projections(
        gate, memory, channel_history=[], episodic_entries=admitted,
    )
    assert episodic == []
    assert gate.manifest(MemoryBudget(total_tokens=1000)) == ()


async def test_a_classification_withhold_still_projects(memory) -> None:
    """The complement, so the terminal rule above cannot silently become
    '§E is dead': an entry withheld for its LEVEL still gets its
    projection, audience check running or not."""
    await replace_entry_projections(
        memory, entry_id="ix-2", entry_tier=ENTRY_TIER_EPISODE,
        projections={"public": "A roadmap decision was made."}, created_at=100.0,
    )
    gate = TurnInjectionGate(
        acting="public", agent_id="test-agent", audience=_audience(),
    )
    episode = _episode("ep-2", interaction_id="ix-2")
    admitted = gate.filter_entries("episodic", [episode])
    assert admitted == []

    _, episodic = await apply_episode_projections(
        gate, memory, channel_history=[], episodic_entries=admitted,
    )
    assert [e.summary for e in episodic] == ["A roadmap decision was made."]


async def test_the_section_g_watch_sees_the_audience_withhold(memory) -> None:
    """One decision record, three readers: the executor-side §G check
    gets the audience-withheld entry's fingerprint for free, because the
    check ran inside the gate rather than in front of it."""
    gate = _gate()
    gate.filter_entries("episodic", [_episode("ep-3", interaction_id="ix-3")])
    event = AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE, payload={}, channel_id=ROOM,
    )
    stamp_turn_tripwire_watch(event, gate)

    watch = tripwire_watch_from_event(event)
    assert watch is not None
    assert [e.entry_id for e in watch.entries] == ["ep-3"]
    # ...and the gate names the cause the watch cannot carry.
    assert [r.verdict.value for r in gate.audience_records] == ["withhold-disjoint"]


async def test_shadow_leaves_every_reader_exactly_where_v0315_left_it(
    memory,
) -> None:
    """The byte-identity claim, from the other three readers' side: in
    shadow the entry injects, §E has nothing to serve, and the watch is
    empty — the v0.3.15 shape."""
    gate = _gate(AUDIENCE_SHADOW)
    episode = _episode("ep-4", interaction_id="ix-4")
    admitted = gate.filter_entries("episodic", [episode])
    assert admitted == [episode]

    event = AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE, payload={}, channel_id=ROOM,
    )
    stamp_turn_tripwire_watch(event, gate)
    assert tripwire_watch_from_event(event) is None
    assert [r.verdict.value for r in gate.audience_records] == ["withhold-disjoint"]
