"""The room's description reaches fact recall on a real turn (ISSUE-0180).

:mod:`test_room_topic_seeds` pins the seeding rule.  This module pins
the wiring around it in ``_inject_memory_context``: the roster, which
carries the description, has to be resolved before the facts are
recalled, and a turn with no description has to behave as before.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from agents.persona import create_persona_agent
from agents.persona_runtime.channel_roster import (
    resolve_channel_roster,
    room_description,
)
from agents.persona_runtime.facts_section import FACTS_SECTION_NAME
from agents.persona_runtime.facts_shadow import (
    CROSS_ROOM_SHADOW,
    SHADOW_LOGGER_NAME,
    SHADOW_TRACE_ATTR,
)
from agents.persona_types import AgentEvent, EventType

from ._channel_roster_helpers import _AGENTS, _CHANNEL, _DM, _FakeFetcher
from ._persona_test_helpers import _PERSONA_CONFIG, _make_client

# ``_CHANNEL``'s description is "engineering + product planning discussion".
ROOM_SUBJECT = "product planning"
FACT = "roadmap review moved to friday"


async def _agent_with_fact(fetcher: _FakeFetcher | None):
    agent = create_persona_agent(
        agent_id="iron-fox", config=_PERSONA_CONFIG, llm_client=_make_client(),
    )
    await agent.initialize_memory()
    if fetcher is not None:
        agent.set_roster_fetcher(fetcher)
    assert agent._fact_store is not None
    await agent._fact_store.store(
        subject=ROOM_SUBJECT, predicate="topic.has_deadline", object=FACT,
        source_interaction_id="ix-1", asserted_at=1000.0,
    )
    return agent


def _turn(channel_id: str, content: str = "hello, shall we start?") -> AgentEvent:
    return AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload={"content": content},
        sender_id="alice",
        channel_id=channel_id,
        metadata={"channel_classification": "internal"},
    )


async def _facts_text(agent, event: AgentEvent) -> str:
    await agent._inject_memory_context(event)
    section = agent._working_memory.get_section(FACTS_SECTION_NAME)
    await agent.close_memory()
    return section.content if section is not None else ""


class TestRoomDescriptionSeedsFactRecall:
    async def test_a_fact_under_the_rooms_subject_reaches_the_prompt(self) -> None:
        """The message names nothing the store holds.  The room's
        description names the subject, so its fact is in the prompt."""
        agent = await _agent_with_fact(_FakeFetcher(_CHANNEL, _AGENTS))
        text = await _facts_text(agent, _turn("group:planning"))
        assert f"- {ROOM_SUBJECT} topic.has_deadline {FACT}" in text

    async def test_a_room_with_no_description_recalls_as_before(self) -> None:
        agent = await _agent_with_fact(_FakeFetcher(_DM, _AGENTS))
        assert await _facts_text(agent, _turn("dm:alice:iron-fox")) == ""

    async def test_a_turn_with_no_roster_recalls_as_before(self) -> None:
        agent = await _agent_with_fact(None)
        assert await _facts_text(agent, _turn("group:planning")) == ""

    async def test_a_lost_roster_costs_the_seed_not_the_turn(self) -> None:
        agent = await _agent_with_fact(_FakeFetcher(RuntimeError("orchestrator down")))
        assert await _facts_text(agent, _turn("group:planning")) == ""

    async def test_the_message_still_seeds_where_the_room_does_not(self) -> None:
        agent = await _agent_with_fact(_FakeFetcher(_DM, _AGENTS))
        text = await _facts_text(
            agent, _turn("dm:alice:iron-fox", "any news on product planning?"),
        )
        assert FACT in text

    async def test_facts_turned_off_still_resolves_the_roster_for_the_gate(self) -> None:
        """The description is only wanted for fact recall, but the roster is
        also the audience the gate reads, so it resolves either way."""
        fetcher = _FakeFetcher(_CHANNEL, _AGENTS)
        agent = await _agent_with_fact(fetcher)
        agent._facts_enabled = False
        assert await _facts_text(agent, _turn("group:planning")) == ""
        assert fetcher.calls == ["group:planning"]

    async def test_shadow_mode_reports_the_room_seeded_fact_and_keeps_it_out(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Under ``cross_room: shadow`` the prompt keeps the room wall and
        the shadow pass logs what the widened read would add.  That pass
        gets the room's description too, so a fact another room taught
        about this room's subject is in the trace and not in the prompt."""
        agent = await _agent_with_fact(_FakeFetcher(_CHANNEL, _AGENTS))
        agent._facts_cross_room = CROSS_ROOM_SHADOW
        assert agent._fact_store is not None
        await agent._fact_store.store(
            subject=ROOM_SUBJECT, predicate="topic.decided", object="taught elsewhere",
            source_interaction_id="ix-2", asserted_at=2000.0, session_id="another-room",
        )
        with caplog.at_level(logging.INFO, logger=SHADOW_LOGGER_NAME):
            text = await _facts_text(agent, _turn("group:planning"))
        assert FACT in text and "taught elsewhere" not in text
        (trace,) = [getattr(r, SHADOW_TRACE_ATTR) for r in caplog.records
                    if hasattr(r, SHADOW_TRACE_ATTR)]
        assert [(c["subject"], c["session_id"]) for c in trace["candidates"]] == [
            (ROOM_SUBJECT, "another-room"),
        ]


class TestRoomDescription:
    async def _roster(self, meta: dict[str, Any]):
        return await resolve_channel_roster(
            _FakeFetcher(meta, _AGENTS), _turn("group:planning"), "iron-fox",
        )

    async def test_reads_the_description_as_configured(self) -> None:
        assert room_description(await self._roster(_CHANNEL)) == _CHANNEL["description"]

    async def test_none_without_a_roster_or_a_description(self) -> None:
        assert room_description(None) is None
        no_description = {k: v for k, v in _CHANNEL.items() if k != "description"}
        assert room_description(await self._roster(no_description)) is None

    async def test_none_when_the_description_is_not_text(self) -> None:
        """It comes off a REST body and feeds the turn's memory read."""
        for bad in (42, ["a", "b"], {"text": "x"}):
            assert room_description(await self._roster({**_CHANNEL, "description": bad})) is None
