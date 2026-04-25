"""
Tests for AgentServiceServicer RPCs: ExecuteTask, HealthCheck, ExecuteTaskStream.

All tests use in-process mocks — no real API calls.
"""

import asyncio
import json
from unittest.mock import MagicMock

import grpc
import grpc.aio
import pytest

from agents.base import BaseAgent, TaskInput, TaskOutput, TaskStatus
from agents.generated import task_pb2
from agents.server import AgentServiceServicer


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
