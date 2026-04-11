"""
Tests for AgentServiceServicer, load_agent, and AgentServer.

All tests use in-process gRPC client and mock agents — no real API calls.
"""

import asyncio
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import grpc
import grpc.aio
import pytest
import yaml

from agents.base import BaseAgent, TaskInput, TaskOutput, TaskStatus
from agents.generated import task_pb2, task_pb2_grpc
from agents.server import (
    AgentServer,
    AgentServiceServicer,
    _resolve_agent_type,
    load_agent,
)
from agents.coder import CoderAgent
from agents.planner_agent import PlannerAgent
from agents.reviewer import ReviewerAgent


# ─── Helpers ─────────────────────────────────────────────────


class _StubAgent(BaseAgent):
    """Minimal agent for servicer tests."""

    async def handle(self, task: TaskInput) -> TaskOutput:
        return TaskOutput(
            status=TaskStatus.COMPLETED,
            result="stub result",
            metadata={"tokens_used": "42"},
        )


class _FailingAgent(BaseAgent):
    """Agent that returns FAILED status."""

    async def handle(self, task: TaskInput) -> TaskOutput:
        return TaskOutput(
            status=TaskStatus.FAILED,
            result="something went wrong",
        )


class _SlowAgent(BaseAgent):
    """Agent that blocks forever (for timeout tests)."""

    async def handle(self, task: TaskInput) -> TaskOutput:
        await asyncio.sleep(3600)
        return TaskOutput(status=TaskStatus.COMPLETED, result="never reached")


class _ExplodingAgent(BaseAgent):
    """Agent whose handle() raises an unexpected exception."""

    async def handle(self, task: TaskInput) -> TaskOutput:
        raise RuntimeError("kaboom")


class _UnhealthyAgent(BaseAgent):
    """Agent that reports unhealthy."""

    async def handle(self, task: TaskInput) -> TaskOutput:
        return TaskOutput(status=TaskStatus.COMPLETED, result="ok")

    async def health_check(self) -> bool:
        return False


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


# ─── AgentServiceServicer Tests ─────────────────────────────


class TestExecuteTask:
    """Tests for AgentServiceServicer.ExecuteTask."""

    async def test_success(self):
        agent = _StubAgent(agent_id="test-agent", config={"model": "test"})
        servicer = AgentServiceServicer({"test-agent": agent})
        context = MagicMock(spec=grpc.aio.ServicerContext)

        resp = await servicer.ExecuteTask(_task_request(), context)

        assert resp.task_id == "t1"
        assert resp.status == task_pb2.COMPLETED
        assert resp.result == "stub result"
        assert resp.error_message == ""
        assert "duration_ms" in resp.metadata
        assert resp.metadata["tokens_used"] == "42"

    async def test_agent_not_found(self):
        servicer = AgentServiceServicer({})
        context = MagicMock(spec=grpc.aio.ServicerContext)

        resp = await servicer.ExecuteTask(
            _task_request(agent_id="nonexistent"), context
        )

        assert resp.status == task_pb2.FAILED
        assert "not found" in resp.error_message.lower()
        context.set_code.assert_called_once_with(grpc.StatusCode.NOT_FOUND)

    async def test_failed_status(self):
        agent = _FailingAgent(agent_id="test-agent", config={"model": "test"})
        servicer = AgentServiceServicer({"test-agent": agent})
        context = MagicMock(spec=grpc.aio.ServicerContext)

        resp = await servicer.ExecuteTask(_task_request(), context)

        assert resp.status == task_pb2.FAILED
        assert resp.error_message == "something went wrong"

    async def test_timeout(self):
        agent = _SlowAgent(agent_id="test-agent", config={"model": "test"})
        servicer = AgentServiceServicer({"test-agent": agent})
        context = MagicMock(spec=grpc.aio.ServicerContext)

        # Use a very short timeout
        req = _task_request(timeout_seconds=1)
        resp = await servicer.ExecuteTask(req, context)

        assert resp.status == task_pb2.FAILED
        assert "timed out" in resp.error_message.lower()
        context.set_code.assert_called_once_with(grpc.StatusCode.DEADLINE_EXCEEDED)

    async def test_agent_exception(self):
        agent = _ExplodingAgent(agent_id="test-agent", config={"model": "test"})
        servicer = AgentServiceServicer({"test-agent": agent})
        context = MagicMock(spec=grpc.aio.ServicerContext)

        resp = await servicer.ExecuteTask(_task_request(), context)

        assert resp.status == task_pb2.FAILED
        # S-11: exception message should NOT contain the raw str(exc)
        assert "kaboom" not in resp.error_message
        assert "RuntimeError" in resp.error_message

    async def test_metadata_json_serialization(self):
        """Non-string metadata values are serialized with json.dumps."""

        class _MetaAgent(BaseAgent):
            async def handle(self, task: TaskInput) -> TaskOutput:
                return TaskOutput(
                    status=TaskStatus.COMPLETED,
                    result="ok",
                    metadata={"count": "5", "nested": json.dumps({"a": 1})},
                )

        agent = _MetaAgent(agent_id="test-agent", config={"model": "test"})
        servicer = AgentServiceServicer({"test-agent": agent})
        context = MagicMock(spec=grpc.aio.ServicerContext)

        resp = await servicer.ExecuteTask(_task_request(), context)

        assert resp.metadata["count"] == "5"
        assert json.loads(resp.metadata["nested"]) == {"a": 1}

    async def test_context_passed_through(self):
        """Verify request.context is forwarded to TaskInput."""
        received_context = {}

        class _ContextAgent(BaseAgent):
            async def handle(self, task: TaskInput) -> TaskOutput:
                received_context.update(task.context)
                return TaskOutput(status=TaskStatus.COMPLETED, result="ok")

        agent = _ContextAgent(agent_id="test-agent", config={"model": "test"})
        servicer = AgentServiceServicer({"test-agent": agent})
        context = MagicMock(spec=grpc.aio.ServicerContext)

        req = task_pb2.TaskRequest(
            task_id="t1",
            workflow_id="w1",
            agent_id="test-agent",
            payload="test",
            context={"prev_step": "result from step 1"},
            config=task_pb2.TaskConfig(),
        )
        await servicer.ExecuteTask(req, context)

        assert received_context["prev_step"] == "result from step 1"

    async def test_task_config_passed_through(self):
        """Verify TaskConfig fields are forwarded to TaskInputConfig."""
        received_config = None

        class _ConfigAgent(BaseAgent):
            async def handle(self, task: TaskInput) -> TaskOutput:
                nonlocal received_config
                received_config = task.config
                return TaskOutput(status=TaskStatus.COMPLETED, result="ok")

        agent = _ConfigAgent(agent_id="test-agent", config={"model": "test"})
        servicer = AgentServiceServicer({"test-agent": agent})
        context = MagicMock(spec=grpc.aio.ServicerContext)

        req = task_pb2.TaskRequest(
            task_id="t1",
            workflow_id="w1",
            agent_id="test-agent",
            payload="test",
            config=task_pb2.TaskConfig(
                max_llm_calls=5,
                max_tokens=2048,
                allowed_tools=["file_read", "shell_exec"],
            ),
        )
        await servicer.ExecuteTask(req, context)

        assert received_config is not None
        assert received_config.max_llm_calls == 5
        assert received_config.max_tokens == 2048
        assert received_config.allowed_tools == ["file_read", "shell_exec"]


class TestHealthCheck:
    """Tests for AgentServiceServicer.HealthCheck."""

    async def test_healthy_agent(self):
        agent = _StubAgent(agent_id="test-agent", config={})
        servicer = AgentServiceServicer({"test-agent": agent})
        context = MagicMock(spec=grpc.aio.ServicerContext)

        resp = await servicer.HealthCheck(
            task_pb2.HealthCheckRequest(service="test-agent"), context
        )
        assert resp.status == task_pb2.SERVING

    async def test_unhealthy_agent(self):
        agent = _UnhealthyAgent(agent_id="test-agent", config={})
        servicer = AgentServiceServicer({"test-agent": agent})
        context = MagicMock(spec=grpc.aio.ServicerContext)

        resp = await servicer.HealthCheck(
            task_pb2.HealthCheckRequest(service="test-agent"), context
        )
        assert resp.status == task_pb2.NOT_SERVING

    async def test_no_specific_agent_healthy(self):
        """No service specified, at least one agent loaded → SERVING."""
        agent = _StubAgent(agent_id="test-agent", config={})
        servicer = AgentServiceServicer({"test-agent": agent})
        context = MagicMock(spec=grpc.aio.ServicerContext)

        resp = await servicer.HealthCheck(
            task_pb2.HealthCheckRequest(), context
        )
        assert resp.status == task_pb2.SERVING

    async def test_no_agents_loaded(self):
        servicer = AgentServiceServicer({})
        context = MagicMock(spec=grpc.aio.ServicerContext)

        resp = await servicer.HealthCheck(
            task_pb2.HealthCheckRequest(), context
        )
        assert resp.status == task_pb2.NOT_SERVING


class TestExecuteTaskStream:
    """Tests for the unimplemented streaming RPC."""

    async def test_returns_unimplemented(self):
        servicer = AgentServiceServicer({})
        context = MagicMock(spec=grpc.aio.ServicerContext)

        await servicer.ExecuteTaskStream(_task_request(), context)

        context.set_code.assert_called_once_with(grpc.StatusCode.UNIMPLEMENTED)


# ─── load_agent Tests ────────────────────────────────────────


def _write_agent_config(tmp: Path, agents: list[dict]) -> str:
    """Write a temporary agents.yaml and return its path."""
    config_path = tmp / "agents.yaml"
    config_path.write_text(
        yaml.dump({"schema_version": "0.1", "agents": agents}),
        encoding="utf-8",
    )
    return str(config_path)


class TestLoadAgent:
    """Tests for load_agent()."""

    @patch("agents.server.create_provider")
    def test_load_planner(self, mock_create):
        mock_create.return_value = MagicMock()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = _write_agent_config(tmp_path, [
                {
                    "id": "planner",
                    "name": "Planner",
                    "role": "Plans things",
                    "model": "test-model",
                    "capabilities": ["planning"],
                    "tools": [],
                    "permissions": {},
                },
            ])
            agent = load_agent("planner", config_path, tmp)
            assert isinstance(agent, PlannerAgent)
            assert agent.agent_id == "planner"

    @patch("agents.server.create_provider")
    def test_load_coder(self, mock_create):
        mock_create.return_value = MagicMock()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = _write_agent_config(tmp_path, [
                {
                    "id": "code-writer",
                    "name": "Code Writer",
                    "role": "Writes code",
                    "model": "test-model",
                    "capabilities": ["code_generation", "code_review"],
                    "tools": ["file_read", "file_write"],
                    "permissions": {},
                },
            ])
            agent = load_agent("code-writer", config_path, tmp)
            assert isinstance(agent, CoderAgent)

    @patch("agents.server.create_provider")
    def test_load_reviewer(self, mock_create):
        mock_create.return_value = MagicMock()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = _write_agent_config(tmp_path, [
                {
                    "id": "code-reviewer",
                    "name": "Code Reviewer",
                    "role": "Reviews code",
                    "model": "test-model",
                    "capabilities": ["code_review", "security_audit"],
                    "tools": ["file_read"],
                    "permissions": {},
                },
            ])
            agent = load_agent("code-reviewer", config_path, tmp)
            assert isinstance(agent, ReviewerAgent)

    def test_missing_config_file(self):
        with pytest.raises(SystemExit, match="not found"):
            load_agent("planner", "/nonexistent/agents.yaml", "/workspace")

    def test_missing_agent_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = _write_agent_config(tmp_path, [
                {"id": "other", "capabilities": ["planning"], "permissions": {}},
            ])
            with pytest.raises(SystemExit, match="not found"):
                load_agent("planner", config_path, tmp)

    def test_bad_yaml(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad_path = Path(tmp) / "agents.yaml"
            bad_path.write_text(": : : invalid yaml {{", encoding="utf-8")
            with pytest.raises(SystemExit, match="Invalid YAML"):
                load_agent("planner", str(bad_path), tmp)

    def test_unknown_capabilities(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = _write_agent_config(tmp_path, [
                {"id": "mystery", "capabilities": ["unknown_cap"], "permissions": {}},
            ])
            with pytest.raises(SystemExit, match="Cannot determine agent type"):
                load_agent("mystery", config_path, tmp)


class TestResolveAgentType:
    """Tests for _resolve_agent_type()."""

    def test_planning_capability(self):
        assert _resolve_agent_type({"id": "x", "capabilities": ["planning"]}) == PlannerAgent

    def test_code_generation_capability(self):
        assert _resolve_agent_type({"id": "x", "capabilities": ["code_generation"]}) == CoderAgent

    def test_code_review_capability(self):
        assert _resolve_agent_type({"id": "x", "capabilities": ["code_review"]}) == ReviewerAgent

    def test_code_writer_with_both_caps(self):
        """code-writer has both code_generation and code_review → CoderAgent."""
        assert _resolve_agent_type(
            {"id": "x", "capabilities": ["code_generation", "code_review"]}
        ) == CoderAgent

    def test_no_matching_capabilities(self):
        with pytest.raises(SystemExit):
            _resolve_agent_type({"id": "x", "capabilities": ["unknown"]})


# ─── AgentServer Tests ───────────────────────────────────────


class TestAgentServer:
    """Tests for AgentServer start/stop lifecycle."""

    async def test_start_and_stop(self):
        """Server starts and stops without error."""
        server = AgentServer(host="127.0.0.1", port=0, shutdown_grace=1)
        agent = _StubAgent(agent_id="test-agent", config={})
        server.register_agent(agent)

        await server.start()
        assert server._server is not None
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

        await server.start()
        await server.stop()

        agent.shutdown.assert_awaited_once()


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
