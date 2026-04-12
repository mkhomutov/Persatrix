"""
Integration tests for the agent gRPC server.

End-to-end tests using in-process gRPC server with mock LLM.
No real API calls or external dependencies.
"""

from unittest.mock import AsyncMock, MagicMock

import grpc
import grpc.aio
import pytest

from agents.generated import task_pb2, task_pb2_grpc
from agents.llm_client import (
    LLMClient,
    LLMResponse,
    StopReason,
    ToolCall,
    Usage,
)
from agents.server import AgentServiceServicer
from agents.task_agent import TaskAgent
from agents.tools.registry import ToolResult, clear_registry, tool


# ─── Helpers ─────────────────────────────────────────────────


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
            return_value=LLMResponse(text="default response")
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


def _task_request(
    agent_id: str = "code-writer",
    payload: str = "Write a hello world script",
    timeout_seconds: int = 0,
) -> task_pb2.TaskRequest:
    return task_pb2.TaskRequest(
        task_id="t1",
        workflow_id="w1",
        agent_id=agent_id,
        payload=payload,
        config=task_pb2.TaskConfig(timeout_seconds=timeout_seconds),
    )


_DEFAULT_CONFIG: dict = {
    "model": "test-model",
    "role": "Test role",
    "max_llm_calls": 10,
    "max_tokens": 4096,
    "tools": [],
    "instructions": "You are a test agent.",
}


# ─── End-to-End Task Execution ──────────────────────────────


class TestEndToEndExecution:
    """Full round-trip: gRPC client → server → agent → LLM mock → response."""

    async def test_coder_agent_success(self):
        """TaskAgent (coder role) receives task, calls mock LLM, returns COMPLETED."""
        client = _make_client(
            responses=[
                LLMResponse(
                    text="Here is the code:\n```python\nprint('hi')\n```",
                    stop_reason=StopReason.END_TURN,
                    usage=Usage(50, 100),
                )
            ]
        )
        agent = TaskAgent(
            agent_id="code-writer",
            config=_DEFAULT_CONFIG,
            llm_client=client,
        )

        servicer = AgentServiceServicer({"code-writer": agent})
        server = grpc.aio.server()
        task_pb2_grpc.add_AgentServiceServicer_to_server(servicer, server)
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()

        try:
            channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
            stub = task_pb2_grpc.AgentServiceStub(channel)

            resp = await stub.ExecuteTask(_task_request())

            assert resp.task_id == "t1"
            assert resp.status == task_pb2.COMPLETED
            assert "code" in resp.result.lower()
            assert "duration_ms" in resp.metadata

            await channel.close()
        finally:
            await server.stop(grace=0)

    async def test_reviewer_agent_success(self):
        """TaskAgent (reviewer role) returns structured review."""
        review_text = '{"approved": true, "issues": [], "summary": "Looks good."}'
        client = _make_client(
            responses=[
                LLMResponse(
                    text=review_text,
                    stop_reason=StopReason.END_TURN,
                    usage=Usage(80, 60),
                )
            ]
        )
        agent = TaskAgent(
            agent_id="code-reviewer",
            config=_DEFAULT_CONFIG,
            llm_client=client,
        )

        servicer = AgentServiceServicer({"code-reviewer": agent})
        server = grpc.aio.server()
        task_pb2_grpc.add_AgentServiceServicer_to_server(servicer, server)
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()

        try:
            channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
            stub = task_pb2_grpc.AgentServiceStub(channel)

            resp = await stub.ExecuteTask(
                _task_request(
                    agent_id="code-reviewer",
                    payload="Review this code: def add(a, b): return a + b",
                )
            )

            assert resp.status == task_pb2.COMPLETED
            assert "approved" in resp.result

            await channel.close()
        finally:
            await server.stop(grace=0)

    async def test_planner_agent_success(self):
        """TaskAgent (planner role) returns execution plan."""
        plan_text = '{"steps": [{"id": 1, "description": "Setup"}], "summary": "Plan"}'
        client = _make_client(
            responses=[
                LLMResponse(
                    text=plan_text,
                    stop_reason=StopReason.END_TURN,
                    usage=Usage(60, 80),
                )
            ]
        )
        agent = TaskAgent(
            agent_id="planner",
            config=_DEFAULT_CONFIG,
            llm_client=client,
        )

        servicer = AgentServiceServicer({"planner": agent})
        server = grpc.aio.server()
        task_pb2_grpc.add_AgentServiceServicer_to_server(servicer, server)
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()

        try:
            channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
            stub = task_pb2_grpc.AgentServiceStub(channel)

            resp = await stub.ExecuteTask(
                _task_request(agent_id="planner", payload="Plan a web app")
            )

            assert resp.status == task_pb2.COMPLETED
            assert "steps" in resp.result

            await channel.close()
        finally:
            await server.stop(grace=0)

    async def test_task_failure(self):
        """Mock LLM raises → task returns FAILED."""
        client = _make_client()
        client._provider.create_message = AsyncMock(
            side_effect=RuntimeError("API error")
        )
        agent = TaskAgent(
            agent_id="code-writer",
            config=_DEFAULT_CONFIG,
            llm_client=client,
        )

        servicer = AgentServiceServicer({"code-writer": agent})
        server = grpc.aio.server()
        task_pb2_grpc.add_AgentServiceServicer_to_server(servicer, server)
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()

        try:
            channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
            stub = task_pb2_grpc.AgentServiceStub(channel)

            resp = await stub.ExecuteTask(_task_request())

            assert resp.status == task_pb2.FAILED

            await channel.close()
        finally:
            await server.stop(grace=0)

    async def test_agent_not_found(self):
        """Request for non-existent agent → NOT_FOUND gRPC error."""
        servicer = AgentServiceServicer({})
        server = grpc.aio.server()
        task_pb2_grpc.add_AgentServiceServicer_to_server(servicer, server)
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()

        try:
            channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
            stub = task_pb2_grpc.AgentServiceStub(channel)

            with pytest.raises(grpc.aio.AioRpcError) as exc_info:
                await stub.ExecuteTask(
                    _task_request(agent_id="nonexistent")
                )
            assert exc_info.value.code() == grpc.StatusCode.NOT_FOUND

            await channel.close()
        finally:
            await server.stop(grace=0)

    async def test_task_with_tool_use(self):
        """End-to-end: agent calls tool during execution."""

        @tool(name="file_write", description="Write a file")
        async def file_write(path: str, content: str) -> ToolResult:
            return ToolResult(success=True, data=f"Wrote {path}")

        tool_call = ToolCall(
            id="tc1",
            name="file_write",
            input={"path": "main.py", "content": "print('hi')"},
        )
        responses = [
            LLMResponse(
                text=None,
                tool_calls=[tool_call],
                stop_reason=StopReason.TOOL_USE,
                usage=Usage(30, 20),
            ),
            LLMResponse(
                text="Created main.py with hello world.",
                stop_reason=StopReason.END_TURN,
                usage=Usage(40, 30),
            ),
        ]
        config = {**_DEFAULT_CONFIG, "tools": ["file_write"]}
        client = _make_client(responses=responses)
        agent = TaskAgent(
            agent_id="code-writer",
            config=config,
            llm_client=client,
        )

        servicer = AgentServiceServicer({"code-writer": agent})
        server = grpc.aio.server()
        task_pb2_grpc.add_AgentServiceServicer_to_server(servicer, server)
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()

        try:
            channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
            stub = task_pb2_grpc.AgentServiceStub(channel)

            resp = await stub.ExecuteTask(_task_request())

            assert resp.status == task_pb2.COMPLETED
            assert resp.metadata["tool_calls"] == "1"

            await channel.close()
        finally:
            await server.stop(grace=0)


class TestEmptyModelGuard:
    """S-18: empty model string guard in create_provider()."""

    def test_empty_model_raises_system_exit(self):
        from agents.llm_client import create_provider

        with pytest.raises(SystemExit, match="'model' field is empty"):
            create_provider({"model": ""})
