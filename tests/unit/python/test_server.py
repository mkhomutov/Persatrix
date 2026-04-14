"""
Tests for AgentServiceServicer, load_agent, and AgentServer.

All tests use in-process gRPC client and mock agents — no real API calls.
"""

import asyncio
import json
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
from agents.task_agent import TaskAgent
from agents.tools import builtin


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
        # S-01: exception type name should NOT be leaked either —
        # only a fixed "Internal error" string is returned.
        assert "RuntimeError" not in resp.error_message
        assert resp.error_message == "Internal error"

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


    async def test_zero_timeout_means_no_timeout(self):
        """SF-07: timeout_seconds=0 (proto default) must not impose a timeout.

        ``0 or None`` evaluates to ``None``, so ``asyncio.wait_for(..., timeout=None)``
        waits indefinitely.  If the code broke and passed ``timeout=0``, the task
        would raise ``TimeoutError`` immediately.
        """
        agent = _StubAgent(agent_id="test-agent", config={"model": "test"})
        servicer = AgentServiceServicer({"test-agent": agent})
        context = MagicMock(spec=grpc.aio.ServicerContext)

        req = _task_request(timeout_seconds=0)
        resp = await servicer.ExecuteTask(req, context)
        assert resp.status == task_pb2.COMPLETED


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

    async def test_unknown_agent_returns_not_serving(self):
        """F-01: requesting health for an unloaded agent ID returns NOT_SERVING."""
        agent = _StubAgent(agent_id="loaded-agent", config={})
        servicer = AgentServiceServicer({"loaded-agent": agent})
        context = MagicMock(spec=grpc.aio.ServicerContext)

        resp = await servicer.HealthCheck(
            task_pb2.HealthCheckRequest(service="nonexistent"), context
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
                    "type": "task",
                    "capabilities": ["planning"],
                    "tools": [],
                    "permissions": {},
                },
            ])
            agent = load_agent("planner", config_path, tmp)
            assert isinstance(agent, TaskAgent)
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
                    "type": "task",
                    "capabilities": ["code_generation", "code_review"],
                    "tools": ["file_read", "file_write"],
                    "permissions": {},
                },
            ])
            agent = load_agent("code-writer", config_path, tmp)
            assert isinstance(agent, TaskAgent)

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
                    "type": "task",
                    "capabilities": ["code_review", "security_audit"],
                    "tools": ["file_read"],
                    "permissions": {},
                },
            ])
            agent = load_agent("code-reviewer", config_path, tmp)
            assert isinstance(agent, TaskAgent)

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

    @patch("agents.server.create_provider")
    def test_unknown_type_raises_system_exit(self, mock_create):
        """Unknown agent type in config raises SystemExit (not ValueError)."""
        mock_create.return_value = MagicMock()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = _write_agent_config(tmp_path, [
                {
                    "id": "mystery",
                    "type": "banana",
                    "model": "test-model",
                    "permissions": {},
                },
            ])
            with pytest.raises(SystemExit, match="Unknown agent type"):
                load_agent("mystery", config_path, tmp)

    def test_agent_entry_missing_id_field(self):
        """F-03: agent config entry without 'id' gives a clear SystemExit."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = _write_agent_config(tmp_path, [
                {"name": "No ID Agent", "capabilities": ["planning"]},
            ])
            with pytest.raises(SystemExit, match="missing required 'id' field"):
                load_agent("planner", config_path, tmp)

    def test_invalid_agent_id_format(self):
        """MF-02: agent IDs must match ^[a-z0-9][a-z0-9-]*[a-z0-9]$."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = _write_agent_config(tmp_path, [
                {"id": "Valid", "capabilities": ["planning"], "permissions": {}},
            ])
            with pytest.raises(SystemExit, match="Invalid agent ID"):
                load_agent("UPPER_CASE", config_path, tmp)

    def test_single_char_agent_id_accepted(self):
        """F-6a-2: single character ID is now valid per updated regex."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = _write_agent_config(tmp_path, [
                {"id": "a", "capabilities": ["planning"], "permissions": {}},
            ])
            # 'a' is a valid ID now, so it should fail for missing model, not ID
            with pytest.raises(SystemExit, match="missing required 'model' field"):
                load_agent("a", config_path, tmp)

    def test_trailing_hyphen_agent_id_rejected(self):
        """Agent ID ending with hyphen fails the regex."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = _write_agent_config(tmp_path, [
                {"id": "agent-", "capabilities": ["planning"], "permissions": {}},
            ])
            with pytest.raises(SystemExit, match="Invalid agent ID"):
                load_agent("agent-", config_path, tmp)

    @patch("agents.server.create_provider")
    def test_missing_model_field(self, mock_create):
        """SF-08: missing 'model' key gives a clear SystemExit at startup."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = _write_agent_config(tmp_path, [
                {
                    "id": "no-model",
                    "name": "No Model Agent",
                    "capabilities": ["planning"],
                    "permissions": {},
                },
            ])
            with pytest.raises(SystemExit, match="missing required 'model' field"):
                load_agent("no-model", config_path, tmp)

    def test_agents_key_not_a_list(self):
        """S-02: 'agents' value that is not a list gives a clear SystemExit."""
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "agents.yaml"
            config_path.write_text(
                "schema_version: '0.1'\nagents: not_a_list\n",
                encoding="utf-8",
            )
            with pytest.raises(SystemExit, match="must be a list"):
                load_agent("planner", str(config_path), tmp)


class TestResolveAgentType:
    """Tests for _resolve_agent_type() — type-based dispatch (RFC 0005 PR 1a)."""

    def test_type_task_explicit(self):
        assert _resolve_agent_type({"id": "x", "type": "task"}) == "task"

    def test_type_default_is_task(self):
        """Agents without a type field default to task (backward compat)."""
        assert _resolve_agent_type({"id": "x"}) == "task"

    def test_type_persona(self):
        """PersonaAgent type resolves to 'persona' string."""
        assert _resolve_agent_type({"id": "x", "type": "persona"}) == "persona"

    def test_unknown_type_raises_system_exit(self):
        """Unknown type values must produce a clean operator-facing SystemExit."""
        with pytest.raises(SystemExit, match="Unknown agent type"):
            _resolve_agent_type({"id": "x", "type": "banana"})


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


# ─── Follow-Up Finding Tests ────────────────────────────────


class TestToolDefinitionFiltering:
    """S-15: direct test for _build_tool_definitions() filtering by agent config."""

    def test_filters_to_configured_tools(self):
        """Agent with tools=['file_read'] only sees file_read, not other tools."""
        from agents.tools.registry import clear_registry, tool, ToolResult

        clear_registry()

        @tool(name="file_read", description="Read a file")
        async def file_read(path: str) -> ToolResult:
            return ToolResult(success=True, data="content")

        @tool(name="shell_exec", description="Run command")
        async def shell_exec(command: str) -> ToolResult:
            return ToolResult(success=True, data="output")

        agent = _StubAgent(
            agent_id="test-agent",
            config={"tools": ["file_read"]},
        )
        defs = agent._build_tool_definitions()

        assert len(defs) == 1
        assert defs[0]["name"] == "file_read"

        clear_registry()

    def test_empty_tools_list_returns_no_tools(self):
        """Agent with tools=[] (e.g. PlannerAgent) sees no tools."""
        from agents.tools.registry import clear_registry, tool, ToolResult

        clear_registry()

        @tool(name="file_read", description="Read a file")
        async def file_read(path: str) -> ToolResult:
            return ToolResult(success=True, data="content")

        agent = _StubAgent(
            agent_id="test-agent",
            config={"tools": []},
        )
        defs = agent._build_tool_definitions()
        assert defs == []

        clear_registry()

    def test_no_tools_key_returns_no_tools(self):
        """Agent without 'tools' key in config sees no tools."""
        agent = _StubAgent(agent_id="test-agent", config={})
        defs = agent._build_tool_definitions()
        assert defs == []


class TestPermissionWiring:
    """S-16: verify load_agent wires permission_gate and path_validator."""

    @patch("agents.server.create_provider")
    def test_permissions_wired_after_load(self, mock_create):
        """After load_agent(), builtin.permission_gate and builtin.path_validator are set."""
        mock_create.return_value = MagicMock()

        original_gate = builtin.permission_gate
        original_validator = builtin.path_validator
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                config_path = _write_agent_config(tmp_path, [
                    {
                        "id": "code-writer",
                        "name": "Code Writer",
                        "model": "test-model",
                        "capabilities": ["code_generation"],
                        "tools": ["file_read"],
                        "permissions": {
                            "filesystem": {
                                "read": ["/workspace/**"],
                                "write": ["/workspace/**"],
                                "deny": ["/etc/**"],
                            },
                            "network": {
                                "allow": ["api.example.com"],
                            },
                        },
                    },
                ])
                load_agent("code-writer", config_path, tmp)

                assert builtin.permission_gate is not None
                assert builtin.path_validator is not None
        finally:
            builtin.permission_gate = original_gate
            builtin.path_validator = original_validator


class TestDuplicateAgentId:
    """S-17: duplicate agent IDs in config are detected."""

    def test_duplicate_agent_id_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = _write_agent_config(tmp_path, [
                {
                    "id": "code-writer",
                    "name": "Code Writer",
                    "model": "test-model",
                    "capabilities": ["code_generation"],
                    "permissions": {},
                },
                {
                    "id": "code-writer",
                    "name": "Code Writer Dupe",
                    "model": "test-model",
                    "capabilities": ["code_generation"],
                    "permissions": {},
                },
            ])
            with pytest.raises(SystemExit, match="Duplicate agent ID"):
                load_agent("code-writer", config_path, tmp)
