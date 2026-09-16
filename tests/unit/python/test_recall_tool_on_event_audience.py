"""ISSUE-0158 — the acting channel reaches the recall client through the REAL turn.

``test_recall_tool_audience.py`` enters the scope by hand. This pins the one
production line that binds it for a live turn — ``on_event`` entering
``request_scope_from_metadata(event.metadata, channel_id=event.channel_id)``
— by driving the real handler with a scripted LLM that elects
``recall_channel_messages``, the :mod:`test_scope_threading_in_loop_parity`
shape. A refactor that moves scope entry and forgets ``channel_id=`` flips
this red instead of shipping the DM-transcript leak silently.
"""

from __future__ import annotations

from typing import Any

import pytest

from agents.channel_event_classification import CHANNEL_CLASSIFICATION_METADATA_KEY
from agents.llm_client import LLMResponse, StopReason, ToolCall, Usage
from agents.persona import create_persona_agent
from agents.persona_types import AgentEvent, EventType
from agents.tools.permissions import PermissionGate
from agents.tools.recall import create_recall_tool
from agents.tools.registry import clear_registry

from ._persona_test_helpers import _PERSONA_CONFIG, _make_client


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


class _RecordingRecallClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def recall(
        self, *, participant_id: str, acting_classification: str, query: str,
        channel_id: str = "", sender: str = "", limit: int = 10,
        acting_channel_id: str = "",
    ) -> list[dict[str, Any]] | None:
        self.calls.append({"acting_channel_id": acting_channel_id, "query": query})
        return []


async def test_on_event_binds_the_acting_channel_for_the_recall_round() -> None:
    responses = [
        LLMResponse(
            text=None,
            tool_calls=[ToolCall(id="tc1", name="recall_channel_messages",
                                 input={"query": "helix"})],
            stop_reason=StopReason.TOOL_USE, usage=Usage(100, 50),
        ),
        LLMResponse(text="Nothing on helix.", stop_reason=StopReason.END_TURN,
                    usage=Usage(200, 100)),
    ]
    client = _make_client(responses)
    recall = _RecordingRecallClient()
    agent = create_persona_agent(
        agent_id="ember-owl", config={**_PERSONA_CONFIG}, llm_client=client,
    )
    await agent.initialize_memory()
    try:
        agent.add_recall_tool(create_recall_tool(
            recall, PermissionGate({"channels": {"recall": True}}),
            agent_id="ember-owl", audience_live=True,
        ))
        await agent.on_event(AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "where did helix land?", "respond_policy": "always"},
            channel_id="group:planning",
            sender_id="alice",
            metadata={CHANNEL_CLASSIFICATION_METADATA_KEY: "internal"},
        ))
    finally:
        await agent.close_memory()
    assert recall.calls, "the scripted turn must reach the recall client"
    assert recall.calls[0]["acting_channel_id"] == "group:planning"
