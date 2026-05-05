"""
End-to-end integration tests for persona agents.

Tests the full cycle: event dispatch → LLM call → action execution → memory store
using in-process agents with mock LLM.  No real API calls or external dependencies.
"""

from unittest.mock import AsyncMock, MagicMock

import grpc
import grpc.aio
import pytest

from agents.generated import task_pb2, task_pb2_grpc
from agents.llm_client import LLMClient, LLMResponse, StopReason, Usage
from agents.dispatch import EventDispatcher
from agents.persona import create_persona_agent
from agents.persona_runtime import _LLMPersonaAgent
from agents.persona_types import ActionType, AgentEvent, EventType
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


# ─── End-to-End Persona Task Execution via gRPC ─────────────


class TestPersonaGrpcExecution:
    """Persona agent handles tasks through the gRPC servicer."""

    async def test_persona_task_via_grpc(self):
        """Persona agent receives a task via gRPC and returns COMPLETED."""
        agent = _create_persona(
            responses=[
                LLMResponse(
                    text='```json\n[{"action_type": "complete_task", "payload": {"result": "Done!"}}]\n```',
                    stop_reason=StopReason.END_TURN,
                    usage=Usage(50, 100),
                ),
            ],
        )
        await agent.initialize_memory()

        try:
            servicer = AgentServiceServicer({"test-persona": agent})
            server = grpc.aio.server()
            task_pb2_grpc.add_AgentServiceServicer_to_server(servicer, server)
            port = server.add_insecure_port("127.0.0.1:0")
            await server.start()

            try:
                channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
                stub = task_pb2_grpc.AgentServiceStub(channel)

                resp = await stub.ExecuteTask(
                    task_pb2.TaskRequest(
                        task_id="t1",
                        workflow_id="w1",
                        agent_id="test-persona",
                        payload="Analyze this data",
                        config=task_pb2.TaskConfig(),
                    )
                )

                assert resp.status == task_pb2.COMPLETED
                assert "Done!" in resp.result

                await channel.close()
            finally:
                await server.stop(grace=0)
        finally:
            await agent.close_memory()


# ─── Event → Action → Memory Cycle ──────────────────────────


class TestEventActionMemoryCycle:
    """Full event → LLM → action → memory store cycle."""

    async def test_event_triggers_memory_store(self):
        """An event triggers on_event(), and the episode is stored in memory."""
        agent = _create_persona(
            responses=[
                LLMResponse(
                    text='```json\n[{"action_type": "complete_task", "payload": {"result": "Analyzed"}}]\n```',
                    stop_reason=StopReason.END_TURN,
                    usage=Usage(30, 50),
                ),
            ],
        )
        await agent.initialize_memory()

        try:
            event = AgentEvent(
                event_type=EventType.TASK_ASSIGNED,
                payload={"task": "Analyze data"},
            )
            actions = await agent.on_event(event)

            assert len(actions) >= 1
            assert actions[0].action_type == ActionType.COMPLETE_TASK

            # Episode should be stored in episodic memory
            episodes = await agent._episodic_memory.recall("Analyze", limit=5)
            assert len(episodes) >= 1
        finally:
            await agent.close_memory()

    async def test_dispatch_event_executes_actions(self):
        """EventDispatcher routes event, executes resulting actions."""
        agent = _create_persona(
            agent_id="agent-a",
            config={**_PERSONA_CONFIG, "id": "agent-a"},
            responses=[
                LLMResponse(
                    text='```json\n[{"action_type": "do_nothing", "payload": {}}]\n```',
                    stop_reason=StopReason.END_TURN,
                    usage=Usage(30, 20),
                ),
            ],
        )
        await agent.initialize_memory()

        try:
            dispatcher = EventDispatcher(agents={"agent-a": agent})
            event = AgentEvent(
                event_type=EventType.CHANNEL_MESSAGE,
                payload={"content": "Hello"},
                sender_id="external",
            )

            actions = await dispatcher.dispatch("agent-a", event)

            assert len(actions) >= 1
            assert actions[0].action_type == ActionType.DO_NOTHING
        finally:
            await agent.close_memory()


# ─── Cross-Agent Event Routing ───────────────────────────────


class TestCrossAgentRouting:
    """Events routed between persona agents via EventDispatcher."""

    async def test_send_message_routes_between_agents(self):
        """Agent A sends a message mentioning Agent B → B receives it."""
        agent_a = _create_persona(
            agent_id="agent-a",
            config={**_PERSONA_CONFIG, "id": "agent-a"},
            responses=[
                LLMResponse(
                    text=(
                        '```json\n[{"action_type": "send_channel_message", '
                        '"payload": {"content": "Hi B!", "mentions": ["agent-b"], '
                        '"channel_id": "general"}}]\n```'
                    ),
                    stop_reason=StopReason.END_TURN,
                    usage=Usage(30, 50),
                ),
            ],
        )
        agent_b = _create_persona(
            agent_id="agent-b",
            config={**_PERSONA_CONFIG, "id": "agent-b"},
            responses=[
                LLMResponse(
                    text='```json\n[{"action_type": "do_nothing", "payload": {}}]\n```',
                    stop_reason=StopReason.END_TURN,
                    usage=Usage(20, 20),
                ),
            ],
        )
        await agent_a.initialize_memory()
        await agent_b.initialize_memory()

        try:
            dispatcher = EventDispatcher(
                agents={"agent-a": agent_a, "agent-b": agent_b},
            )

            # Trigger Agent A with a task → it sends message to Agent B
            event = AgentEvent(
                event_type=EventType.TASK_ASSIGNED,
                payload={"task": "Tell Agent B hello"},
            )
            actions = await dispatcher.dispatch("agent-a", event)

            # Agent A should have returned SEND_CHANNEL_MESSAGE
            assert any(a.action_type == ActionType.SEND_CHANNEL_MESSAGE for a in actions)

            # Agent B should have been called via the dispatcher cascade
            # (on_event is called by ActionExecutor → EventDispatcher)
            assert agent_b._llm_client._provider.create_message.call_count >= 1
        finally:
            await agent_a.close_memory()
            await agent_b.close_memory()

    async def test_cascade_depth_limits_cross_agent_chain(self):
        """A → B → A chain terminates when cascade depth reaches max_cascade_depth.

        Agent A sends message mentioning B, B replies mentioning A, and so on.
        With max_cascade_depth=3, the chain should terminate without infinite
        recursion.  Validates full-stack cascade limiting (not just the unit-level
        depth check in EventDispatcher).
        (PR #55 review: cascade depth integration test.)
        """
        # Each agent always replies mentioning the other
        reply_a = LLMResponse(
            text=(
                '```json\n[{"action_type": "send_channel_message", '
                '"payload": {"content": "Reply from A", "mentions": ["agent-b"], '
                '"channel_id": "general"}}]\n```'
            ),
            stop_reason=StopReason.END_TURN,
            usage=Usage(20, 30),
        )
        reply_b = LLMResponse(
            text=(
                '```json\n[{"action_type": "send_channel_message", '
                '"payload": {"content": "Reply from B", "mentions": ["agent-a"], '
                '"channel_id": "general"}}]\n```'
            ),
            stop_reason=StopReason.END_TURN,
            usage=Usage(20, 30),
        )

        # Provide enough responses for max possible cascade invocations
        agent_a = _create_persona(
            agent_id="agent-a",
            config={**_PERSONA_CONFIG, "id": "agent-a"},
            responses=[reply_a for _ in range(10)],
        )
        agent_b = _create_persona(
            agent_id="agent-b",
            config={**_PERSONA_CONFIG, "id": "agent-b"},
            responses=[reply_b for _ in range(10)],
        )
        await agent_a.initialize_memory()
        await agent_b.initialize_memory()

        try:
            dispatcher = EventDispatcher(
                agents={"agent-a": agent_a, "agent-b": agent_b},
                max_cascade_depth=3,
            )

            # Kick off the cascade
            event = AgentEvent(
                event_type=EventType.TASK_ASSIGNED,
                payload={"task": "Start a conversation with Agent B"},
            )
            actions = await dispatcher.dispatch("agent-a", event)

            # Chain should complete (not hang or raise)
            assert isinstance(actions, list)

            # Total LLM calls across both agents should be bounded by
            # cascade depth (at most 3 dispatches, each triggers one LLM call)
            total_calls = (
                agent_a._llm_client._provider.create_message.call_count
                + agent_b._llm_client._provider.create_message.call_count
            )
            assert total_calls <= 3, (
                f"Expected at most 3 LLM calls (cascade depth=3), got {total_calls}"
            )
        finally:
            await agent_a.close_memory()
            await agent_b.close_memory()
