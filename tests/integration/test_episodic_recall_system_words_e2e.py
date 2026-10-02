"""A message sharing only system words with the store recalls nothing (ISSUE-0159).

Episodic recall matches any word a message shares with an episode. Every
closed conversation stores its bookkeeping in the searched context column
("close_reason", "participant_type", "channel_message", …), and every
single-turn event stores "Event: … → Actions: […]" as its summary. An
agent's message reaches recall as "Message from X: …", so the word
"message" alone used to bring back every stored conversation. This drives a
real persona through a conversation and a task, then checks:

* every word the runtime wrote into those rows is on
  ``EPISODE_STRUCTURAL_WORDS``, apart from the names, the task's own words
  and numbers;
* a message sharing only those words recalls nothing; and
* a message naming the task's content still finds the task, whose words
  live only in the stored event.
"""

from __future__ import annotations

import json
import re
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.llm_client import LLMClient, LLMResponse, StopReason, Usage
from agents.memory._fts5_query import EPISODE_STRUCTURAL_WORDS, FTS5_STOPWORDS
from agents.persona import create_persona_agent
from agents.persona_runtime import _LLMPersonaAgent
from agents.persona_runtime.summarize_close import SUMMARIZATION_MAX_OUTPUT_TOKENS
from agents.persona_types import AgentEvent, EventType
from agents.tools.registry import clear_registry

#: The words the test itself put into the rows (names and the task's text),
#: and "u2192", the JSON escape of the summary line's arrow, which no
#: message contains.
_OWN_WORDS = {
    "iron", "fox", "harbour", "orchestrator", "draft", "lantern", "budget", "u2192",
}

_CONFIG: dict = {
    "id": "system-words-persona",
    "model": "test-model",
    "role": "Persona for the system-words recall test",
    "type": "persona",
    "max_llm_calls": 5,
    # Distinct from the summariser's cap so the mock can tell the calls apart.
    "max_tokens": 4096,
    "tools": [],
    "persona": {
        "name": "System Words Agent",
        "background": "A persona used by the ISSUE-0159 system-words test.",
        "behavior": {
            "directness": "balanced",
            "formality": "professional",
            "risk_tolerance": "moderate",
        },
    },
    "autonomy": {
        "level": "semi-autonomous",
        "tick_interval_seconds": 1,
        "max_actions_per_tick": 3,
        "idle_after_ticks": 5,
    },
    "memory": {"db_path": ":memory:", "working": {"max_tokens": 50000}},
    "relationships": [],
}


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


def _client() -> LLMClient:
    """Turns reply with one do_nothing action; the close-path summariser
    returns a summary that shares no word with the test's messages."""
    provider = AsyncMock()

    async def _route(*, model, messages, system, tools, max_tokens, temperature):
        if max_tokens == SUMMARIZATION_MAX_OUTPUT_TOKENS:
            text = json.dumps({"summary": "Greetings exchanged.", "facts": []})
        else:
            text = '```json\n[{"action_type": "do_nothing", "payload": {}}]\n```'
        return LLMResponse(text=text, stop_reason=StopReason.END_TURN, usage=Usage(10, 5))

    provider.create_message = AsyncMock(side_effect=_route)
    provider.format_tool_definitions = MagicMock(return_value=[])
    provider.append_tool_round = MagicMock(side_effect=lambda msgs, resp, results: msgs)
    return LLMClient(provider)


async def _agent_with_a_conversation_and_a_task() -> _LLMPersonaAgent:
    agent = create_persona_agent(
        agent_id=_CONFIG["id"], config=_CONFIG, llm_client=_client(),
    )
    await agent.initialize_memory()
    for content, metadata in (
        ("hello there", {"sender_participant_type": "user"}),
        ("bye", {"chat_end": True}),
    ):
        await agent.on_event(AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE, payload={"content": content},
            sender_id="iron-fox", channel_id="harbour", metadata=metadata,
        ))
    await agent.on_event(AgentEvent(
        event_type=EventType.TASK_ASSIGNED,
        payload={"task": "Draft the lantern budget"}, sender_id="orchestrator",
    ))
    await agent.drain_pending_summaries()
    return agent


async def _stored(agent: _LLMPersonaAgent) -> list[tuple[str, str, str]]:
    db = agent._episodic_memory._ensure_db()
    async with db.execute(
        "SELECT id, summary, context_json FROM episodes WHERE agent_id = ?",
        (agent.agent_id,),
    ) as cursor:
        return [(r[0], r[1], r[2]) for r in await cursor.fetchall()]


def _message_from(sender: str, content: str) -> str:
    """The text the persona hands recall for an agent's channel message."""
    return f"Message from {sender}:\n\n{content}"


@pytest.mark.asyncio
async def test_system_words_recall_nothing_and_content_still_finds_the_task():
    agent = await _agent_with_a_conversation_and_a_task()
    rows = await _stored(agent)
    (task_id,) = [i for i, summary, _ in rows if summary.startswith("Event: task_assigned")]
    assert len(rows) == 2

    written = set()
    for _, summary, context in rows:
        written |= {w.lower() for w in re.findall(r"[A-Za-z0-9]+", f"{summary} {context}")}
    unlisted = {
        w for w in written - _OWN_WORDS - FTS5_STOPWORDS - EPISODE_STRUCTURAL_WORDS
        if not w.isdigit()
    }
    # The closed conversation's summary is the mock's text, not the runtime's.
    assert unlisted == {"greetings", "exchanged"}

    memory = agent._episodic_memory
    assert await memory.recall(
        _message_from("cobalt-wren", "Any thoughts on the event summary?"),
        min_score=0.20,
    ) == []
    got = await memory.recall(
        _message_from("cobalt-wren", "How is the lantern budget going?"),
        min_score=0.20,
    )
    assert [ep.id for ep in got] == [task_id]
