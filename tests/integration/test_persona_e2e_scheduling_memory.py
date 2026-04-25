"""
End-to-end integration tests for persona agents.

Tests the full cycle: event dispatch → LLM call → action execution → memory store
using in-process agents with mock LLM.  No real API calls or external dependencies.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import grpc
import grpc.aio
import pytest

from agents.generated import task_pb2, task_pb2_grpc
from agents.llm_client import LLMClient, LLMResponse, StopReason, Usage
from agents.dispatch import ActionExecutor, EventDispatcher
from agents.persona import create_persona_agent
from agents.persona_runtime import _LLMPersonaAgent
from agents.persona_types import ActionType, AgentEvent, EventType
from agents.tick import TickScheduler
from agents.server import AgentServiceServicer
from agents.tools.registry import clear_registry


# ─── Helpers ─────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


_PERSONA_CONFIG: dict = {
    "id": "test-persona",
    "model": "test-model",
    "role": "Test persona",
    "type": "persona",
    "max_llm_calls": 10,
    "max_tokens": 4096,
    "tools": [],
    "persona": {
        "name": "Test Agent",
        "background": "A test persona for integration tests.",
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
    "memory": {
        "db_path": ":memory:",
        "working": {"max_tokens": 50000},
    },
    "relationships": [],
}


def _make_client(
    responses: list[LLMResponse] | None = None,
) -> LLMClient:
    """Create a mock LLMClient that returns the given responses."""
    mock_provider = AsyncMock()
    if responses:
        mock_provider.create_message = AsyncMock(side_effect=responses)
    else:
        mock_provider.create_message = AsyncMock(
            return_value=LLMResponse(
                text='```json\n[{"action_type": "do_nothing", "payload": {}}]\n```',
                stop_reason=StopReason.END_TURN,
                usage=Usage(30, 20),
            ),
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


def _create_persona(
    agent_id: str = "test-persona",
    config: dict | None = None,
    responses: list[LLMResponse] | None = None,
) -> _LLMPersonaAgent:
    """Create a persona agent with mock LLM."""
    cfg = config or {**_PERSONA_CONFIG, "id": agent_id}
    client = _make_client(responses)
    agent = create_persona_agent(
        agent_id=agent_id,
        config=cfg,
        llm_client=client,
    )
    assert isinstance(agent, _LLMPersonaAgent)
    return agent


# ─── Tick Scheduler Integration ──────────────────────────────


class TestTickSchedulerIntegration:
    """Tick scheduler fires on_tick() with real asyncio timing."""

    @pytest.fixture(autouse=True)
    def _lower_min_interval(self):
        """Allow sub-second intervals in tests.

        Production _MIN_INTERVAL is 1.0s to prevent cost bursts
        (F-64-DR2-11).  Tests need fast intervals to avoid multi-second waits.
        """
        original = TickScheduler._MIN_INTERVAL
        TickScheduler._MIN_INTERVAL = 0.01
        yield
        TickScheduler._MIN_INTERVAL = original

    async def test_tick_fires_and_stops(self):
        """Scheduler fires at least one tick and stops gracefully."""
        agent = _create_persona(
            responses=[
                # Return do_nothing for each tick
                LLMResponse(
                    text='```json\n[{"action_type": "do_nothing", "payload": {}}]\n```',
                    stop_reason=StopReason.END_TURN,
                    usage=Usage(10, 10),
                ),
                LLMResponse(
                    text='```json\n[{"action_type": "do_nothing", "payload": {}}]\n```',
                    stop_reason=StopReason.END_TURN,
                    usage=Usage(10, 10),
                ),
                LLMResponse(
                    text='```json\n[{"action_type": "do_nothing", "payload": {}}]\n```',
                    stop_reason=StopReason.END_TURN,
                    usage=Usage(10, 10),
                ),
            ],
        )
        await agent.initialize_memory()

        try:
            executor = ActionExecutor()
            scheduler = TickScheduler(
                agent,
                interval=0.1,
                idle_after_ticks=100,  # Don't go idle during test
                executor=executor,
            )
            scheduler.start()

            # Let a few ticks fire
            await asyncio.sleep(0.35)

            await scheduler.stop(timeout=5.0)

            assert not scheduler.is_running
            # At least one tick should have been processed.
            # RFC 0017 §F: a fresh agent with empty memory short-circuits before
            # the LLM call and returns DO_NOTHING, so idle_count is the reliable
            # signal that the scheduler fired rather than create_message.call_count.
            assert scheduler.idle_count >= 1
        finally:
            await agent.close_memory()

    async def test_wake_resets_idle_and_resumes_ticks(self):
        """wake() resets idle counter and allows ticks to fire again."""

        agent = _create_persona(
            responses=[
                # All ticks return do_nothing
                LLMResponse(
                    text='```json\n[{"action_type": "do_nothing", "payload": {}}]\n```',
                    stop_reason=StopReason.END_TURN,
                    usage=Usage(10, 10),
                )
                for _ in range(20)
            ],
        )
        await agent.initialize_memory()

        try:
            executor = ActionExecutor()
            scheduler = TickScheduler(
                agent,
                interval=0.05,
                idle_after_ticks=2,  # Go idle after 2 DO_NOTHING ticks
                executor=executor,
            )
            scheduler.start()

            # Let it go idle
            await asyncio.sleep(0.25)
            assert scheduler.idle_count >= 2

            # Wake it up
            scheduler.wake()
            assert scheduler.idle_count == 0

            # Let ticks fire again
            await asyncio.sleep(0.15)

            await scheduler.stop(timeout=5.0)
        finally:
            await agent.close_memory()


# ─── Memory Lifecycle Integration ────────────────────────────


class TestMemoryLifecycleIntegration:
    """Memory tiers initialized and closed correctly in end-to-end flow."""

    async def test_memory_survives_event_cycle(self):
        """Episodes stored during events survive across multiple events."""
        agent = _create_persona(
            responses=[
                LLMResponse(
                    text='```json\n[{"action_type": "complete_task", "payload": {"result": "First done"}}]\n```',
                    stop_reason=StopReason.END_TURN,
                    usage=Usage(30, 50),
                ),
                LLMResponse(
                    text='```json\n[{"action_type": "complete_task", "payload": {"result": "Second done"}}]\n```',
                    stop_reason=StopReason.END_TURN,
                    usage=Usage(30, 50),
                ),
            ],
        )
        await agent.initialize_memory()

        try:
            # First event
            event1 = AgentEvent(
                event_type=EventType.TASK_ASSIGNED,
                payload={"task": "First task alpha"},
            )
            await agent.on_event(event1)

            # Second event
            event2 = AgentEvent(
                event_type=EventType.TASK_ASSIGNED,
                payload={"task": "Second task beta"},
            )
            await agent.on_event(event2)

            # Both episodes should be in episodic memory
            all_episodes = await agent._episodic_memory.recall("task", limit=10)
            assert len(all_episodes) >= 2
        finally:
            await agent.close_memory()

    async def test_cross_agent_memory_isolation(self):
        """Two agents with separate :memory: DBs have isolated episodes."""
        agent_a = _create_persona(
            agent_id="iso-a",
            config={**_PERSONA_CONFIG, "id": "iso-a"},
            responses=[
                LLMResponse(
                    text='```json\n[{"action_type": "complete_task", "payload": {"result": "A done"}}]\n```',
                    stop_reason=StopReason.END_TURN,
                    usage=Usage(30, 50),
                ),
            ],
        )
        agent_b = _create_persona(
            agent_id="iso-b",
            config={**_PERSONA_CONFIG, "id": "iso-b"},
            responses=[
                LLMResponse(
                    text='```json\n[{"action_type": "complete_task", "payload": {"result": "B done"}}]\n```',
                    stop_reason=StopReason.END_TURN,
                    usage=Usage(30, 50),
                ),
            ],
        )
        await agent_a.initialize_memory()
        await agent_b.initialize_memory()

        try:
            await agent_a.on_event(
                AgentEvent(
                    event_type=EventType.TASK_ASSIGNED,
                    payload={"task": "Alpha unique marker"},
                )
            )
            await agent_b.on_event(
                AgentEvent(
                    event_type=EventType.TASK_ASSIGNED,
                    payload={"task": "Beta unique marker"},
                )
            )

            # Agent A should NOT see Agent B's episodes
            a_episodes = await agent_a._episodic_memory.recall("Beta unique", limit=5)
            assert len(a_episodes) == 0

            # Agent B should NOT see Agent A's episodes
            b_episodes = await agent_b._episodic_memory.recall("Alpha unique", limit=5)
            assert len(b_episodes) == 0
        finally:
            await agent_a.close_memory()
            await agent_b.close_memory()
