"""PR #846 — conversation-level close effects fire once per close event.

Split out of ``test_summarize_on_close_phases.py`` for the 500-line cap
(``scripts/checks/file_size.py --strict``).  Pins the ``conversation_lead``
designation (``close_path.persist_fanned_closes`` — the first record of a
close event's fan whose Phase-1 persist scheduled Phase 2) across the
fans, the same-event cap + session-end pairing, and the idle sweep's
per-scope grouping, for both effects: the RFC 0020 §H auto-reflect
tick and the DM relationship bump.
"""

from __future__ import annotations

import time

import pytest

from agents.persona_types import AgentEvent, EventType
from agents.principal_id import principal_scope
from agents.tools.registry import clear_registry

from ._summarize_close_helpers import drain, make_agent


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


@pytest.mark.asyncio
class TestConversationLevelEffectsFirePerCloseEvent:
    """PR #846 review — the finalize's two conversation-level effects
    (the RFC 0020 §H auto-reflect tick, the DM relationship bump) fire
    once per CLOSE EVENT: a room-close fan of N records designates one
    ``conversation_lead``, so neither effect inflates by room size."""

    async def test_room_close_ticks_reflect_counter_once(self):
        agent = await make_agent()
        for speaker in ("alice", "bob", "cara"):
            await agent._store_event_episode(AgentEvent(
                event_type=EventType.CHANNEL_MESSAGE,
                payload={"content": f"hi from {speaker}",
                         "channel_type": "group"},
                channel_id="group:planning",
                sender_id=speaker,
            ), [])

        ticks = 0
        real_increment = agent._episodic_memory.increment_interaction_count

        async def counting_increment():
            nonlocal ticks
            ticks += 1
            await real_increment()

        agent._episodic_memory.increment_interaction_count = counting_increment
        end = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "wrapping up", "channel_type": "group"},
            channel_id="group:planning",
            sender_id="alice",
            metadata={"chat_end": True},
        )
        await agent._store_event_episode(end, [])
        await drain(agent)

        assert ticks == 1, (
            "one room close is one conversation — the reflect counter "
            "must not tick once per (principal, speaker) record"
        )

    async def test_principal_split_dm_close_bumps_relationship_once(self):
        agent = await make_agent()
        peer = "human-pal"
        # The same DM peer under two tenants → two records, one scope
        # (the ISSUE-0123 principal axis).
        for principal in ("alice-person", "bob-person"):
            with principal_scope(principal):
                await agent._store_event_episode(AgentEvent(
                    event_type=EventType.CHANNEL_MESSAGE,
                    payload={"content": f"hello as {principal}"},
                    sender_id=peer,
                ), [])

        bumps: list[dict] = []
        real_record = agent._memory_ns.relationship.record_interaction

        async def counting_record(**kwargs):
            bumps.append(kwargs)
            return await real_record(**kwargs)

        agent._memory_ns.relationship.record_interaction = counting_record
        end = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "bye"},
            sender_id=peer,
            metadata={"chat_end": True},
        )
        await agent._store_event_episode(end, [])
        await drain(agent)

        assert len(bumps) == 1, (
            "one DM conversation ending must bump the peer relationship "
            "once, however many principals split its records"
        )
        assert bumps[0]["other_id"] == peer

    async def test_cap_riding_session_end_is_one_close_event(self):
        """PR #846 re-review: a max-turns close and a session end riding
        the SAME event are one close event — the fan designates a new
        lead only when the cap's persist carried none, so the reflect
        counter ticks once, not twice."""
        agent = await make_agent()
        agent._interaction_tracker._max_turns = 2
        for speaker in ("alice", "bob"):
            await agent._store_event_episode(AgentEvent(
                event_type=EventType.CHANNEL_MESSAGE,
                payload={"content": f"hi from {speaker}",
                         "channel_type": "group"},
                channel_id="group:planning",
                sender_id=speaker,
            ), [])

        ticks = 0
        real_increment = agent._episodic_memory.increment_interaction_count

        async def counting_increment():
            nonlocal ticks
            ticks += 1
            await real_increment()

        agent._episodic_memory.increment_interaction_count = counting_increment
        # alice's SECOND turn is the cap-th turn AND carries the session end.
        end = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "done — wrapping up",
                     "channel_type": "group"},
            channel_id="group:planning",
            sender_id="alice",
            metadata={"chat_end": True},
        )
        await agent._store_event_episode(end, [])
        await drain(agent)

        assert ticks == 1, (
            "the cap close and the same-event session-end fan must share "
            "ONE conversation lead"
        )

    async def test_room_idle_sweep_ticks_reflect_counter_once(self):
        """PR #846 re-review: same-scope records idling out in one sweep
        are one conversation going quiet — one reflect tick, not one per
        (principal, speaker) record."""
        agent = await make_agent()
        for speaker in ("alice", "bob", "cara"):
            await agent._store_event_episode(AgentEvent(
                event_type=EventType.CHANNEL_MESSAGE,
                payload={"content": f"hi from {speaker}",
                         "channel_type": "group"},
                channel_id="group:planning",
                sender_id=speaker,
            ), [])

        ticks = 0
        real_increment = agent._episodic_memory.increment_interaction_count

        async def counting_increment():
            nonlocal ticks
            ticks += 1
            await real_increment()

        agent._episodic_memory.increment_interaction_count = counting_increment
        # Push the tracker's clock past the idle window; the next event's
        # flush sweeps all three records in one pass.
        agent._interaction_tracker._clock = lambda: time.time() + 3_600.0
        await agent._store_event_episode(AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "unrelated"},
            sender_id="someone-else",
        ), [])
        await drain(agent)

        assert ticks == 1, (
            "one room going quiet is one conversation — the idle sweep "
            "leads per scope, not per record"
        )

    async def test_principal_split_dm_idle_bumps_relationship_once(self):
        """PR #846 re-review: idle is how most DMs end — the sweep's
        per-scope lead must cover it, or a principal-split DM double-
        bumps the peer exactly as the fan used to."""
        agent = await make_agent()
        peer = "human-pal"
        for principal in ("alice-person", "bob-person"):
            with principal_scope(principal):
                await agent._store_event_episode(AgentEvent(
                    event_type=EventType.CHANNEL_MESSAGE,
                    payload={"content": f"hello as {principal}"},
                    sender_id=peer,
                ), [])

        bumps: list[dict] = []
        real_record = agent._memory_ns.relationship.record_interaction

        async def counting_record(**kwargs):
            bumps.append(kwargs)
            return await real_record(**kwargs)

        agent._memory_ns.relationship.record_interaction = counting_record
        agent._interaction_tracker._clock = lambda: time.time() + 3_600.0
        await agent._store_event_episode(AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "unrelated"},
            sender_id="someone-else",
        ), [])
        await drain(agent)

        assert len(bumps) == 1, (
            "two principal-split records idling out together are one DM "
            "conversation — one relationship bump"
        )
        assert bumps[0]["other_id"] == peer
