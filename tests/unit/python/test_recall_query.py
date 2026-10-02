"""What episodic and notes recall search for on a persona turn (ISSUE-0159).

Recall matches any word of its query, at most 40 phrases. The prompt text
of a scripted turn (a convener opening, a chair escalation, a synthesis
turn) wraps the message in fixed framing long enough to use up all 40, so
the meeting topic or the stalled question was never searched. Recall now
searches the message's own words; the prompt text stays what the facts
tier reads and what other events search.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from agents.clock import WallClock
from agents.memory.episodic import EpisodicMemory
from agents.memory.working import WorkingMemory
from agents.persona_runtime.cross_room import CROSS_ROOM_LIVE
from agents.persona_runtime.memory_context import _MemoryContextMixin
from agents.persona_runtime.prompt_assembly import _PromptAssemblyMixin
from agents.persona_runtime.recall_query import recall_text_for_event
from agents.persona_types import AgentEvent, EventType

TOPIC = "Topic: quarterly zeppelin procurement budget"


def _message(event_type: EventType = EventType.CHANNEL_MESSAGE, **payload) -> AgentEvent:
    return AgentEvent(
        event_type=event_type, payload=payload, channel_id="group:hangar",
        sender_id="ember-owl",
        metadata={"channel_classification": "internal"},
    )


@pytest.mark.parametrize("event", [
    _message(content="Where are we on the zeppelin?"),
    _message(EventType.MENTION, content="Where are we on the zeppelin?"),
    _message(content=TOPIC, convene=True),
    _message(content="Should we buy the zeppelin?", chair_escalation=True),
])
def test_a_message_is_searched_by_its_own_words(event: AgentEvent):
    assert recall_text_for_event(event, "framed prompt text") == event.payload["content"]


@pytest.mark.parametrize("event", [
    AgentEvent(event_type=EventType.TICK),
    AgentEvent(event_type=EventType.TASK_ASSIGNED, payload={"task": "Draft it"}),
    _message(content="   "),
    _message(content=None),
])
def test_anything_else_is_searched_by_its_prompt_text(event: AgentEvent):
    assert recall_text_for_event(event, "framed prompt text") == "framed prompt text"


class _Persona(_MemoryContextMixin):
    """The memory mixin with the persona's real event formatter."""

    def __init__(self, episodic: EpisodicMemory) -> None:
        super().__init__()
        self.agent_id = "recall-query-agent"
        self._clock = WallClock()
        self._timezone = "UTC"
        self._working_memory = WorkingMemory(max_tokens=8192)
        self._episodic_memory = episodic
        self._relationship_memory = AsyncMock()
        self._relationship_memory.get_relationship_summary.return_value = None
        self._episodic_cross_room = CROSS_ROOM_LIVE

    def _format_event(self, event):  # type: ignore[override]
        return _PromptAssemblyMixin._format_event(None, event)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_convener_opening_recalls_the_meeting_topic():
    episodic = EpisodicMemory(agent_id="recall-query-agent", db_path=":memory:")
    await episodic.initialize()
    try:
        await episodic.store_episode(
            "The briefing set the zeppelin procurement budget at 2 million",
            {"k": "v"}, session_id="room-a", protection_level="internal",
        )
        for i in range(6):
            await episodic.store_episode(
                f"Reviewed the bakery flour order {i}", {"k": "v"},
                session_id="room-a", protection_level="internal",
            )
        persona = _Persona(episodic)
        await persona._inject_memory_context(_message(content=TOPIC, convene=True))
        rendered = "\n".join(s.content for s in persona._working_memory._sections)
        assert "zeppelin procurement budget at 2 million" in rendered
    finally:
        await episodic.close()
