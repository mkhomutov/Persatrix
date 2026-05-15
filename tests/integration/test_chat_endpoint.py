"""
Integration test for POST /api/v1/agents/{id}/chat endpoint.

Full round-trip: HTTP client → Go REST handler → gRPC → Python agent → reply.
Uses in-process gRPC server with mock LLM to avoid external dependencies.
(RFC 0016 PR 4)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import grpc.aio
import pytest

from agents.base import BaseAgent, TaskInput, TaskOutput, TaskStatus
from agents.dispatch import EventDispatcher
from agents.generated import task_pb2, task_pb2_grpc
from agents.persona_types import ActionType, AgentAction
from agents.server_servicers import AgentServiceServicer
from agents.tools.registry import clear_registry


# ─── Helpers ─────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


class _StubAgent(BaseAgent):
    """Minimal agent for integration tests."""

    async def handle(self, task: TaskInput) -> TaskOutput:
        return TaskOutput(status=TaskStatus.COMPLETED, result="ok")


def _make_servicer(
    actions: list[AgentAction],
    agent_id: str = "test-agent",
) -> AgentServiceServicer:
    """Create a servicer with a mock dispatcher that returns *actions*."""
    agent = _StubAgent(agent_id=agent_id, config={"model": "test"})
    dispatcher = MagicMock(spec=EventDispatcher)
    dispatcher.dispatch = AsyncMock(return_value=actions)
    dispatcher.executor = MagicMock()
    dispatcher.executor.execute = AsyncMock(return_value=[])
    return AgentServiceServicer({agent_id: agent}, dispatcher)


class TestChatEndpointIntegration:
    """
    Integration tests for the chat endpoint.

    These tests start a real gRPC agent server and verify the
    SendChatMessage RPC works end-to-end. The Go REST layer
    (handleChat) is tested separately in Go unit tests.

    NOTE: Full REST→gRPC integration requires the Go orchestrator
    to be running. These tests verify the Python gRPC servicer
    via direct gRPC calls, matching what the Go handler dispatches.
    """

    async def test_send_chat_message_round_trip(self):
        """gRPC client → agent servicer → mock dispatcher → reply."""
        actions = [
            AgentAction(
                action_type=ActionType.SEND_CHANNEL_MESSAGE,
                payload={"content": "Hello, human!", "target": "all"},
            ),
        ]
        servicer = _make_servicer(actions, agent_id="test-agent")

        server = grpc.aio.server()
        task_pb2_grpc.add_AgentServiceServicer_to_server(servicer, server)
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()

        try:
            channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
            stub = task_pb2_grpc.AgentServiceStub(channel)

            resp = await stub.SendChatMessage(
                task_pb2.ChatRequest(
                    agent_id="test-agent",
                    user_id="local",
                    message="Hi there!",
                )
            )

            assert resp.reply == "Hello, human!"
            assert resp.reply_status == "ok"
            assert resp.agent_id == "test-agent"
            assert resp.chat_session_id  # Should be non-empty (server-generated)

            await channel.close()
        finally:
            await server.stop(grace=0)

    async def test_chat_empty_reply(self):
        """Agent returns no applicable actions → empty reply with 'empty' status."""
        servicer = _make_servicer([], agent_id="quiet-agent")

        server = grpc.aio.server()
        task_pb2_grpc.add_AgentServiceServicer_to_server(servicer, server)
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()

        try:
            channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
            stub = task_pb2_grpc.AgentServiceStub(channel)

            resp = await stub.SendChatMessage(
                task_pb2.ChatRequest(
                    agent_id="quiet-agent",
                    user_id="local",
                    message="hello?",
                )
            )

            assert resp.reply == ""
            assert resp.reply_status == "empty"

            await channel.close()
        finally:
            await server.stop(grace=0)

    async def test_chat_session_continuity(self):
        """Second message with same chat_session_id continues conversation."""
        actions = [
            AgentAction(
                action_type=ActionType.SEND_CHANNEL_MESSAGE,
                payload={"content": "reply", "target": "all"},
            ),
        ]
        servicer = _make_servicer(actions, agent_id="chat-agent")

        server = grpc.aio.server()
        task_pb2_grpc.add_AgentServiceServicer_to_server(servicer, server)
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()

        try:
            channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
            stub = task_pb2_grpc.AgentServiceStub(channel)

            # First message — get server-generated chat session ID.
            resp1 = await stub.SendChatMessage(
                task_pb2.ChatRequest(
                    agent_id="chat-agent",
                    user_id="local",
                    message="first message",
                )
            )
            chat_session_id = resp1.chat_session_id
            assert chat_session_id

            # Second message — provide same chat session ID.
            resp2 = await stub.SendChatMessage(
                task_pb2.ChatRequest(
                    agent_id="chat-agent",
                    user_id="local",
                    message="second message",
                    chat_session_id=chat_session_id,
                )
            )
            assert resp2.chat_session_id == chat_session_id

            await channel.close()
        finally:
            await server.stop(grace=0)

    async def test_chat_unknown_agent(self):
        """SendChatMessage for unknown agent returns NOT_FOUND."""
        servicer = _make_servicer([], agent_id="known-agent")

        server = grpc.aio.server()
        task_pb2_grpc.add_AgentServiceServicer_to_server(servicer, server)
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()

        try:
            channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
            stub = task_pb2_grpc.AgentServiceStub(channel)

            with pytest.raises(grpc.aio.AioRpcError) as exc_info:
                await stub.SendChatMessage(
                    task_pb2.ChatRequest(
                        agent_id="nonexistent-agent",
                        user_id="local",
                        message="hello",
                    )
                )
            assert exc_info.value.code() == grpc.StatusCode.NOT_FOUND

            await channel.close()
        finally:
            await server.stop(grace=0)
