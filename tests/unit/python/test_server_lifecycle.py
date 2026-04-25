"""
Tests for AgentServer start/stop lifecycle and in-process gRPC integration.

All tests use in-process gRPC — no external network calls.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import grpc
import grpc.aio
import pytest

from agents.base import BaseAgent, TaskInput, TaskOutput, TaskStatus
from agents.generated import task_pb2, task_pb2_grpc
from agents.server import AgentServer, AgentServiceServicer


# ─── Helpers ─────────────────────────────────────────────────


class _StubAgent(BaseAgent):
    """Minimal agent for lifecycle and gRPC tests."""

    async def handle(self, task: TaskInput) -> TaskOutput:
        return TaskOutput(
            status=TaskStatus.COMPLETED,
            result="stub result",
            metadata={"tokens_used": "42"},
        )


def _task_request(
    agent_id: str = "test-agent",
    payload: str = "do something",
    timeout_seconds: int = 0,
) -> task_pb2.TaskRequest:
    return task_pb2.TaskRequest(
        task_id="t1",
        workflow_id="w1",
        agent_id=agent_id,
        payload=payload,
        config=task_pb2.TaskConfig(timeout_seconds=timeout_seconds),
    )


# ─── AgentServer Tests ───────────────────────────────────────


class TestAgentServer:
    """Tests for AgentServer start/stop lifecycle."""

    async def test_start_and_stop(self):
        """Server starts and stops without error."""
        server = AgentServer(host="127.0.0.1", port=0, shutdown_grace=1)
        agent = _StubAgent(agent_id="test-agent", config={})
        server.register_agent(agent)

        with patch.object(server, "_self_register", new_callable=AsyncMock):
            await server.start()
        assert server._server is not None
        # SF-05: when port=0 is used, self.port must be updated to the
        # actual allocated port (not 0).
        assert server.port != 0
        with patch.object(server, "_self_deregister", new_callable=AsyncMock):
            await server.stop()

    async def test_register_agent(self):
        server = AgentServer()
        agent = _StubAgent(agent_id="my-agent", config={"name": "My Agent"})
        server.register_agent(agent)
        assert "my-agent" in server.agents

    async def test_stop_calls_agent_shutdown(self):
        server = AgentServer(host="127.0.0.1", port=0, shutdown_grace=1)
        agent = _StubAgent(agent_id="test-agent", config={})
        agent.shutdown = AsyncMock()
        server.register_agent(agent)

        with patch.object(server, "_self_register", new_callable=AsyncMock):
            await server.start()
        with patch.object(server, "_self_deregister", new_callable=AsyncMock):
            await server.stop()

        agent.shutdown.assert_awaited_once()

    async def test_shutdown_exception_does_not_block_siblings(self):
        """F-02: one agent's shutdown() raising must not prevent others from cleaning up."""
        server = AgentServer(host="127.0.0.1", port=0, shutdown_grace=1)

        agent_a = _StubAgent(agent_id="agent-a", config={})
        agent_a.shutdown = AsyncMock(side_effect=RuntimeError("cleanup failed"))
        agent_b = _StubAgent(agent_id="agent-b", config={})
        agent_b.shutdown = AsyncMock()

        server.register_agent(agent_a)
        server.register_agent(agent_b)

        with patch.object(server, "_self_register", new_callable=AsyncMock):
            await server.start()
        with patch.object(server, "_self_deregister", new_callable=AsyncMock):
            await server.stop()

        agent_a.shutdown.assert_awaited_once()
        agent_b.shutdown.assert_awaited_once()


# ─── In-Process gRPC Integration Tests ──────────────────────


class TestGRPCIntegration:
    """End-to-end tests using an in-process gRPC server and client."""

    async def test_execute_task_via_grpc(self):
        """Full round-trip: client → gRPC server → agent → response."""
        agent = _StubAgent(agent_id="test-agent", config={"model": "test"})
        server = AgentServer(host="127.0.0.1", port=0, shutdown_grace=1)
        server.register_agent(agent)

        # Start server on a random port
        server._server = grpc.aio.server()
        servicer = AgentServiceServicer(server.agents)
        task_pb2_grpc.add_AgentServiceServicer_to_server(servicer, server._server)
        port = server._server.add_insecure_port("127.0.0.1:0")
        await server._server.start()

        try:
            channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
            stub = task_pb2_grpc.AgentServiceStub(channel)

            resp = await stub.ExecuteTask(_task_request())

            assert resp.task_id == "t1"
            assert resp.status == task_pb2.COMPLETED
            assert resp.result == "stub result"
            assert "duration_ms" in resp.metadata

            await channel.close()
        finally:
            await server._server.stop(grace=0)

    async def test_health_check_via_grpc(self):
        agent = _StubAgent(agent_id="my-agent", config={})
        server_obj = grpc.aio.server()
        servicer = AgentServiceServicer({"my-agent": agent})
        task_pb2_grpc.add_AgentServiceServicer_to_server(servicer, server_obj)
        port = server_obj.add_insecure_port("127.0.0.1:0")
        await server_obj.start()

        try:
            channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
            stub = task_pb2_grpc.AgentServiceStub(channel)

            resp = await stub.HealthCheck(
                task_pb2.HealthCheckRequest(service="my-agent")
            )
            assert resp.status == task_pb2.SERVING

            await channel.close()
        finally:
            await server_obj.stop(grace=0)

    async def test_agent_not_found_via_grpc(self):
        server_obj = grpc.aio.server()
        servicer = AgentServiceServicer({})
        task_pb2_grpc.add_AgentServiceServicer_to_server(servicer, server_obj)
        port = server_obj.add_insecure_port("127.0.0.1:0")
        await server_obj.start()

        try:
            channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
            stub = task_pb2_grpc.AgentServiceStub(channel)

            # The servicer returns a FAILED response (not a gRPC error)
            # but also sets gRPC status code for the transport layer.
            with pytest.raises(grpc.aio.AioRpcError) as exc_info:
                await stub.ExecuteTask(
                    _task_request(agent_id="nonexistent")
                )
            assert exc_info.value.code() == grpc.StatusCode.NOT_FOUND

            await channel.close()
        finally:
            await server_obj.stop(grace=0)
