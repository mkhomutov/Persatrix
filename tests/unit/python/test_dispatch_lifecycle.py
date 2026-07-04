"""
Integration tests for the dispatch lifecycle — cross-agent memory isolation,
full event→action→memory cycle, and per-dispatch timeout handling.

All tests use mock LLM client — no real API calls.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.dispatch import ActionExecutor, EventDispatcher
from agents.llm_client import LLMClient, LLMResponse
from agents.persona import create_persona_agent
from agents.persona_runtime import _LLMPersonaAgent
from agents.persona_types import ActionType, AgentAction, AgentEvent, EventType
from agents.tools.registry import clear_registry
from agents.channel_wire_metadata import DispatchContext

# ─── Fixtures ───────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


def _make_client(
    responses: list[LLMResponse] | None = None,
) -> LLMClient:
    """Create a mock LLMClient that returns the given responses."""
    mock_provider = AsyncMock()
    if responses:
        mock_provider.create_message = AsyncMock(side_effect=responses)
    else:
        mock_provider.create_message = AsyncMock(
            return_value=LLMResponse(text="I'll handle this task.")
        )
    mock_provider.format_tool_definitions = MagicMock(return_value=[])
    mock_provider.append_tool_round = MagicMock(
        side_effect=lambda msgs, resp, results: [
            *msgs,
            {"role": "assistant", "content": "tool round"},
            {"role": "user", "content": "tool results"},
        ]
    )
    return LLMClient(mock_provider)


_PERSONA_CONFIG: dict = {
    "id": "ember-owl",
    "type": "persona",
    "name": "Ember Owl",
    "role": "Engineering leadership",
    "model": "test-model",
    "temperature": 0.7,
    "max_llm_calls": 10,
    "max_tokens": 4096,
    "persona": {
        "title": "VP of Engineering",
        "background": "15 years in software engineering.",
        "behavior": {
            "directness": "direct",
            "detail_focus": "big-picture",
            "formality": "professional",
            "risk_tolerance": "moderate",
            "expressiveness": "reserved",
        },
    },
    "permissions": {
        "memory": {"read": True, "write": True},
    },
    "memory": {
        "db_path": ":memory:",
        "notes": {"max_notes": 100, "auto_reflect_after": 5},
    },
}

_PERSONA_CONFIG_2: dict = {
    "id": "iron-fox",
    "type": "persona",
    "name": "Iron Fox",
    "role": "Senior developer",
    "model": "test-model",
    "temperature": 0.7,
    "max_llm_calls": 10,
    "max_tokens": 4096,
    "persona": {
        "title": "Senior Engineer",
        "background": "Full-stack developer.",
        "behavior": {},
    },
    "permissions": {
        "memory": {"read": True, "write": True},
    },
    "memory": {
        "db_path": ":memory:",
        "notes": {"max_notes": 100, "auto_reflect_after": 5},
    },
}


async def _make_agent(
    config: dict | None = None,
    llm_client: LLMClient | None = None,
) -> _LLMPersonaAgent:
    """Helper to create an initialized _LLMPersonaAgent."""
    cfg = config or {**_PERSONA_CONFIG}
    client = llm_client or _make_client()
    agent = create_persona_agent(
        agent_id=cfg["id"], config=cfg, llm_client=client,
    )
    await agent.initialize_memory()
    return agent


# ─── Cross-Agent Memory Isolation ───────────────────────────


class TestCrossAgentMemoryIsolation:
    """Verify that agents sharing the dispatcher cannot access each other's data."""

    async def test_agent_episodes_isolated(self):
        """Agent A's stored episodes are not visible to agent B."""
        agent_a = await _make_agent(config={**_PERSONA_CONFIG})
        agent_b = await _make_agent(config={**_PERSONA_CONFIG_2})

        # Store episode for agent A
        await agent_a._episodic_memory.store_episode(
            summary="Secret A episode", context={"secret": True},
        )

        # Agent B should not see it
        episodes = await agent_b._episodic_memory.recall("Secret A episode")
        assert len(episodes) == 0

        await agent_a.close_memory()
        await agent_b.close_memory()


# ─── Integration: Full Event → Action → Memory Cycle ────────


class TestEventActionMemoryCycle:
    """Full integration: event dispatched → agent processes → episode stored."""

    async def test_full_cycle(self):
        agent = await _make_agent()
        dispatcher = EventDispatcher(agents={"ember-owl": agent})

        event = AgentEvent(
            event_type=EventType.TASK_ASSIGNED,
            payload={"task": "Review code"},
        )
        actions = await dispatcher.dispatch("ember-owl", event)
        assert len(actions) >= 1

        # Verify episode was stored
        episodes = await agent._episodic_memory.recall("task_assigned")
        assert len(episodes) >= 1
        await agent.close_memory()

    async def test_concurrent_dispatch_serialized(self):
        """Concurrent dispatches to the same agent are serialized by the lock."""
        agent = await _make_agent()
        dispatcher = EventDispatcher(agents={"ember-owl": agent})

        events = [
            AgentEvent(
                event_type=EventType.CHANNEL_MESSAGE,
                payload={"content": f"msg-{i}"},
                sender_id="test",
            )
            for i in range(3)
        ]

        # Dispatch all concurrently
        results = await asyncio.gather(
            *[dispatcher.dispatch("ember-owl", e) for e in events]
        )
        # All should complete without error
        assert len(results) == 3
        for r in results:
            assert len(r) >= 1
        await agent.close_memory()


# ─── F-5b-4: Per-dispatch timeout in SEND_CHANNEL_MESSAGE ──────────


class TestPerDispatchTimeout:
    """F-5b-4: _handle_send_channel_message wraps dispatch with asyncio.wait_for."""

    async def test_dispatch_timeout_logged_not_raised(self):
        """A dispatch timeout is caught gracefully — sender is not blocked.

        We mock the dispatcher to raise TimeoutError (what asyncio.wait_for
        raises) to verify the except clause in _handle_send_channel_message.
        """
        agent = await _make_agent()
        dispatcher = EventDispatcher(agents={"ember-owl": agent})
        executor = ActionExecutor(dispatcher=dispatcher)

        # Make dispatch raise TimeoutError as if wait_for expired.
        async def _raise_timeout(target_id, event):
            raise TimeoutError()

        dispatcher.dispatch = _raise_timeout  # type: ignore[assignment]

        action = AgentAction(
            action_type=ActionType.SEND_CHANNEL_MESSAGE,
            payload={
                "content": "Hello",
                "mentions": ["ember-owl"],
            },
        )
        results = await executor.execute("ember-owl", [action], context=DispatchContext(cascade_depth=0))
        assert len(results) == 1
        # Dispatch timed out, so dispatched_to == 0 (timeout is caught, not counted).
        assert results[0]["dispatched_to"] == 0
        # F-60-6: status is "failed" when all dispatches failed (was "dispatched").
        assert results[0]["status"] == "failed"

        await agent.close_memory()
