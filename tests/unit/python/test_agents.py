"""
Tests for CoderAgent, ReviewerAgent, and PlannerAgent.

All tests use mock LLM client — no real API calls.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.base import TaskInput, TaskStatus
from agents.coder import CoderAgent
from agents.llm_client import (
    LLMClient,
    LLMResponse,
    StopReason,
    ToolCall,
    Usage,
)
from agents.planner_agent import PlannerAgent
from agents.reviewer import ReviewerAgent
from agents.tools.registry import ToolResult, clear_registry, tool


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


_DEFAULT_CONFIG: dict = {
    "model": "test-model",
    "role": "Test role description",
    "max_llm_calls": 10,
    "max_tokens": 4096,
}


def _task(payload: str = "do something") -> TaskInput:
    return TaskInput(task_id="t1", workflow_id="w1", payload=payload)


# ─── CoderAgent Tests ───────────────────────────────────────


class TestCoderAgent:
    async def test_handle_returns_completed(self):
        client = _make_client(
            responses=[LLMResponse(text="Here is the code:\n```python\nprint('hi')\n```", stop_reason=StopReason.END_TURN, usage=Usage(50, 100))]
        )
        agent = CoderAgent(agent_id="code-writer", config=_DEFAULT_CONFIG, llm_client=client)
        output = await agent.handle(_task("Write a hello world script"))
        assert output.status == TaskStatus.COMPLETED
        assert "code" in output.result.lower()

    async def test_handle_with_tool_use(self):
        @tool(name="file_write", description="Write a file")
        async def file_write(path: str, content: str) -> ToolResult:
            return ToolResult(success=True, data=f"Wrote {path}")

        tool_call = ToolCall(id="tc1", name="file_write", input={"path": "main.py", "content": "print('hi')"})
        responses = [
            LLMResponse(text=None, tool_calls=[tool_call], stop_reason=StopReason.TOOL_USE, usage=Usage(30, 20)),
            LLMResponse(text="Created main.py with hello world.", stop_reason=StopReason.END_TURN, usage=Usage(40, 30)),
        ]
        client = _make_client(responses=responses)
        agent = CoderAgent(agent_id="code-writer", config=_DEFAULT_CONFIG, llm_client=client)
        output = await agent.handle(_task("Write a hello world script"))
        assert output.status == TaskStatus.COMPLETED
        assert output.metadata["tool_calls"] == "1"

    async def test_system_prompt_includes_role(self):
        config = {**_DEFAULT_CONFIG, "role": "Senior Python developer"}
        client = _make_client()
        agent = CoderAgent(agent_id="code-writer", config=config, llm_client=client)
        await agent.handle(_task())
        call_kwargs = client._provider.create_message.call_args[1]
        assert "Senior Python developer" in call_kwargs["system"]

    async def test_llm_error_returns_failed(self):
        client = _make_client()
        client._provider.create_message = AsyncMock(
            side_effect=RuntimeError("API error")
        )
        agent = CoderAgent(agent_id="code-writer", config=_DEFAULT_CONFIG, llm_client=client)
        output = await agent.handle(_task())
        assert output.status == TaskStatus.FAILED

    async def test_capabilities_from_config(self):
        config = {**_DEFAULT_CONFIG, "capabilities": ["code_generation", "code_review"]}
        agent = CoderAgent(agent_id="code-writer", config=config)
        assert agent.capabilities == ["code_generation", "code_review"]

    async def test_capabilities_empty_without_config(self):
        agent = CoderAgent(agent_id="code-writer", config={"model": "test"})
        assert agent.capabilities == []


# ─── ReviewerAgent Tests ────────────────────────────────────


class TestReviewerAgent:
    async def test_handle_returns_completed(self):
        review_output = '{"approved": true, "issues": [], "summary": "Code looks good."}'
        client = _make_client(
            responses=[LLMResponse(text=review_output, stop_reason=StopReason.END_TURN, usage=Usage(80, 60))]
        )
        agent = ReviewerAgent(agent_id="code-reviewer", config=_DEFAULT_CONFIG, llm_client=client)
        output = await agent.handle(_task("Review this code: def add(a, b): return a + b"))
        assert output.status == TaskStatus.COMPLETED
        assert "approved" in output.result

    async def test_system_prompt_includes_role(self):
        config = {**_DEFAULT_CONFIG, "role": "Security-focused code reviewer"}
        client = _make_client()
        agent = ReviewerAgent(agent_id="code-reviewer", config=config, llm_client=client)
        await agent.handle(_task())
        call_kwargs = client._provider.create_message.call_args[1]
        assert "Security-focused code reviewer" in call_kwargs["system"]

    async def test_llm_error_returns_failed(self):
        client = _make_client()
        client._provider.create_message = AsyncMock(
            side_effect=ConnectionError("timeout")
        )
        agent = ReviewerAgent(agent_id="code-reviewer", config=_DEFAULT_CONFIG, llm_client=client)
        output = await agent.handle(_task())
        assert output.status == TaskStatus.FAILED

    async def test_capabilities_from_config(self):
        config = {**_DEFAULT_CONFIG, "capabilities": ["code_review", "security_audit"]}
        agent = ReviewerAgent(agent_id="code-reviewer", config=config)
        assert agent.capabilities == ["code_review", "security_audit"]

    async def test_handle_with_tool_use(self):
        """S-13: ReviewerAgent tool-use test (parity with CoderAgent)."""

        @tool(name="file_read", description="Read a file")
        async def file_read(path: str) -> ToolResult:
            return ToolResult(success=True, data="def add(a, b): return a + b")

        tool_call = ToolCall(
            id="tc1",
            name="file_read",
            input={"path": "main.py"},
        )
        responses = [
            LLMResponse(
                text=None,
                tool_calls=[tool_call],
                stop_reason=StopReason.TOOL_USE,
                usage=Usage(40, 20),
            ),
            LLMResponse(
                text='{"approved": true, "issues": [], "summary": "Looks good."}',
                stop_reason=StopReason.END_TURN,
                usage=Usage(50, 40),
            ),
        ]
        config = {**_DEFAULT_CONFIG, "tools": ["file_read"]}
        client = _make_client(responses=responses)
        agent = ReviewerAgent(
            agent_id="code-reviewer",
            config=config,
            llm_client=client,
        )
        output = await agent.handle(_task("Review main.py"))
        assert output.status == TaskStatus.COMPLETED
        assert output.metadata["tool_calls"] == "1"


# ─── PlannerAgent Tests ─────────────────────────────────────


class TestPlannerAgent:
    async def test_handle_returns_completed(self):
        plan_output = '{"steps": [{"id": 1, "description": "Set up project", "depends_on": [], "effort": "small"}], "summary": "Simple setup plan"}'
        client = _make_client(
            responses=[LLMResponse(text=plan_output, stop_reason=StopReason.END_TURN, usage=Usage(60, 80))]
        )
        agent = PlannerAgent(agent_id="planner", config=_DEFAULT_CONFIG, llm_client=client)
        output = await agent.handle(_task("Plan a web application project"))
        assert output.status == TaskStatus.COMPLETED
        assert "steps" in output.result

    async def test_system_prompt_includes_role(self):
        config = {**_DEFAULT_CONFIG, "role": "Technical project planner"}
        client = _make_client()
        agent = PlannerAgent(agent_id="planner", config=config, llm_client=client)
        await agent.handle(_task())
        call_kwargs = client._provider.create_message.call_args[1]
        assert "Technical project planner" in call_kwargs["system"]

    async def test_llm_error_returns_failed(self):
        client = _make_client()
        client._provider.create_message = AsyncMock(
            side_effect=RuntimeError("rate limited")
        )
        agent = PlannerAgent(agent_id="planner", config=_DEFAULT_CONFIG, llm_client=client)
        output = await agent.handle(_task())
        assert output.status == TaskStatus.FAILED

    async def test_capabilities_from_config(self):
        config = {**_DEFAULT_CONFIG, "capabilities": ["planning", "decomposition"]}
        agent = PlannerAgent(agent_id="planner", config=config)
        assert agent.capabilities == ["planning", "decomposition"]


# ─── Cross-Agent Tests ──────────────────────────────────────


class TestCrossAgent:
    """Tests that apply to all three agent types."""

    @pytest.mark.parametrize(
        "agent_cls,agent_id",
        [
            (CoderAgent, "code-writer"),
            (ReviewerAgent, "code-reviewer"),
            (PlannerAgent, "planner"),
        ],
    )
    async def test_no_llm_client_returns_failed(self, agent_cls, agent_id):
        agent = agent_cls(agent_id=agent_id, config=_DEFAULT_CONFIG)
        output = await agent.handle(_task())
        assert output.status == TaskStatus.FAILED
        assert "LLM client not configured" in output.result

    @pytest.mark.parametrize(
        "agent_cls,agent_id",
        [
            (CoderAgent, "code-writer"),
            (ReviewerAgent, "code-reviewer"),
            (PlannerAgent, "planner"),
        ],
    )
    async def test_name_from_config(self, agent_cls, agent_id):
        config = {**_DEFAULT_CONFIG, "name": "Custom Name"}
        agent = agent_cls(agent_id=agent_id, config=config)
        assert agent.name == "Custom Name"

    @pytest.mark.parametrize(
        "agent_cls,agent_id",
        [
            (CoderAgent, "code-writer"),
            (ReviewerAgent, "code-reviewer"),
            (PlannerAgent, "planner"),
        ],
    )
    async def test_role_from_config(self, agent_cls, agent_id):
        config = {**_DEFAULT_CONFIG, "role": "Custom Role"}
        agent = agent_cls(agent_id=agent_id, config=config)
        assert agent.role == "Custom Role"

    @pytest.mark.parametrize(
        "agent_cls,agent_id",
        [
            (CoderAgent, "code-writer"),
            (ReviewerAgent, "code-reviewer"),
            (PlannerAgent, "planner"),
        ],
    )
    async def test_token_counting(self, agent_cls, agent_id):
        client = _make_client(
            responses=[LLMResponse(text="done", stop_reason=StopReason.END_TURN, usage=Usage(100, 200))]
        )
        agent = agent_cls(agent_id=agent_id, config=_DEFAULT_CONFIG, llm_client=client)
        output = await agent.handle(_task())
        assert output.status == TaskStatus.COMPLETED
        assert output.metadata["tokens_used"] == "300"
