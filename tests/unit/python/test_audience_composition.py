"""ISSUE-0132 (v0.3.16 PR A2) — how the audience check composes.

Scope lock 3 makes the check a **gate input**, not a pre-filter, so the
three readers of the gate's one decision record each see it correctly.

§E composes in **both** directions: it must not substitute a projection
for an audience withhold (a projection lowers an entry's classification,
which is a different axis from who is in the room), and a projection
served for a CLASSIFICATION withhold — whose entry returned from the
gate before the audience clause ever ran — must be judged before it is
served, or the AND-condition holds for verbatim entries and is open for
their abstractions.

§G composes by **exclusion**: an audience-withheld entry stays off the
tripwire watch.  It cleared the rank comparison, so its level is at or
below the acting level, and ``find_tripwire_hits`` compares no levels at
all — watching it would file every echo of at-level text as a
confidentiality-breach audit record for a boundary §G does not police.

The §G manifest must not label an audience-withheld entry as injected.
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


def _episode(
    episode_id: str, *, interaction_id: str, level: str = "internal",
) -> Episode:
    return Episode(
        id=episode_id, agent_id="test-agent", summary=_SUMMARY, context={},
        outcome=None, importance=0.5, access_count=0, last_accessed_at=None,
        tags=[], created_at=1.0, compressed_at=None, compression_level=0,
        interaction_id=interaction_id,
        protection_level=level,
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


async def test_the_section_g_watch_skips_the_audience_withhold(memory) -> None:
    """§G watches text ABOVE the publish target, and relies on that so
    completely that ``find_tripwire_hits`` compares no levels at all.  An
    audience withhold cleared the rank comparison first, so its level is
    at or below the acting level: watching it would turn every echo of
    at-level text into a confidentiality-breach audit record.  The cause
    is recorded on the gate, which is where the audience axis lives."""
    gate = _gate()
    gate.filter_entries("episodic", [_episode("ep-3", interaction_id="ix-3")])
    event = AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE, payload={}, channel_id=ROOM,
    )
    stamp_turn_tripwire_watch(event, gate)

    assert tripwire_watch_from_event(event) is None
    assert [r.verdict.value for r in gate.audience_records] == ["withhold-disjoint"]


async def test_a_classification_withhold_still_watched_by_section_g(memory) -> None:
    """The complement, so the exclusion above cannot silently disarm §G:
    an entry withheld for its LEVEL is above the target and still rides
    the watch, audience check running or not."""
    gate = _gate()
    gate.filter_entries(
        "episodic", [_episode("ep-5", interaction_id="ix-5", level="secret")],
    )
    event = AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE, payload={}, channel_id=ROOM,
    )
    stamp_turn_tripwire_watch(event, gate)

    watch = tripwire_watch_from_event(event)
    assert watch is not None
    assert [e.entry_id for e in watch.entries] == ["ep-5"]


async def test_a_projection_for_a_rank_withhold_is_audience_judged(memory) -> None:
    """The other half of the AND-condition.  ``admit`` returns at the rank
    comparison, so a classification-withheld entry carries no verdict —
    and §E then re-admits exactly those entries as declassified
    stand-ins.  Abstracting Alice's DM fact to ``internal`` and serving it
    in Bob's room still tells Bob there is such a fact, so the stand-in is
    judged at its own level before it is served."""
    await replace_entry_projections(
        memory, entry_id="ix-6", entry_tier=ENTRY_TIER_EPISODE,
        projections={"internal": "Alice flagged a roadmap decision."},
        created_at=100.0,
    )
    gate = _gate()
    episode = _episode("ep-6", interaction_id="ix-6", level="restricted")
    admitted = gate.filter_entries("episodic", [episode])
    assert admitted == []          # rank, not audience — no verdict yet
    before_projection = gate.audience_records
    assert before_projection == ()

    _, episodic = await apply_episode_projections(
        gate, memory, channel_history=[], episodic_entries=admitted,
    )

    assert episodic == []
    assert [r.verdict.value for r in gate.audience_records] == ["withhold-disjoint"]
    # The stand-in was judged at the PROJECTION's level, not the entry's.
    assert [r.protection_level for r in gate.audience_records] == ["internal"]
    assert gate.audience_withheld_count == 1


async def test_shadow_serves_the_projection_and_still_records_it(memory) -> None:
    """Byte-identity, from the projection path's side: in shadow the
    stand-in is served exactly as in v0.3.15, and the verdict that would
    have stopped it is still recorded — which is what keeps a projected
    entry in the measured denominator it belongs in."""
    await replace_entry_projections(
        memory, entry_id="ix-7", entry_tier=ENTRY_TIER_EPISODE,
        projections={"internal": "Alice flagged a roadmap decision."},
        created_at=100.0,
    )
    gate = _gate(AUDIENCE_SHADOW)
    episode = _episode("ep-7", interaction_id="ix-7", level="restricted")
    admitted = gate.filter_entries("episodic", [episode])

    _, episodic = await apply_episode_projections(
        gate, memory, channel_history=[], episodic_entries=admitted,
    )

    assert [e.summary for e in episodic] == ["Alice flagged a roadmap decision."]
    assert [r.verdict.value for r in gate.audience_records] == ["withhold-disjoint"]
    assert gate.audience_withheld_count == 0


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
