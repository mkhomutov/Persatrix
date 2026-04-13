"""
Tests for TaskAgent — the data-driven task agent replacing CoderAgent,
ReviewerAgent, and PlannerAgent.

All tests use mock LLM client — no real API calls.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.base import TaskInput, TaskStatus
from agents.llm_client import (
    LLMClient,
    LLMResponse,
    StopReason,
    ToolCall,
    Usage,
)
from agents.task_agent import TaskAgent
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


# ─── TaskAgent Core Tests ───────────────────────────────────


class TestTaskAgent:
    async def test_handle_returns_completed(self):
        client = _make_client(
            responses=[LLMResponse(text="Here is the code:\n```python\nprint('hi')\n```", stop_reason=StopReason.END_TURN, usage=Usage(50, 100))]
        )
        config = {**_DEFAULT_CONFIG, "instructions": "Write code."}
        agent = TaskAgent(agent_id="code-writer", config=config, llm_client=client)
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
        config = {**_DEFAULT_CONFIG, "tools": ["file_write"], "instructions": "Write code."}
        client = _make_client(responses=responses)
        agent = TaskAgent(agent_id="code-writer", config=config, llm_client=client)
        output = await agent.handle(_task("Write a hello world script"))
        assert output.status == TaskStatus.COMPLETED
        assert output.metadata["tool_calls"] == "1"

    async def test_system_prompt_includes_role(self):
        config = {**_DEFAULT_CONFIG, "role": "Senior Python developer", "instructions": "Write clean code."}
        client = _make_client()
        agent = TaskAgent(agent_id="code-writer", config=config, llm_client=client)
        await agent.handle(_task())
        call_kwargs = client._provider.create_message.call_args[1]
        assert "Senior Python developer" in call_kwargs["system"]

    async def test_system_prompt_includes_instructions(self):
        config = {**_DEFAULT_CONFIG, "instructions": "Write idiomatic, production-quality code."}
        client = _make_client()
        agent = TaskAgent(agent_id="code-writer", config=config, llm_client=client)
        await agent.handle(_task())
        call_kwargs = client._provider.create_message.call_args[1]
        assert "Write idiomatic, production-quality code." in call_kwargs["system"]

    async def test_system_prompt_without_instructions(self):
        """TaskAgent without instructions still functions — system prompt is just the role."""
        config = {**_DEFAULT_CONFIG}
        client = _make_client()
        agent = TaskAgent(agent_id="generic", config=config, llm_client=client)
        await agent.handle(_task())
        call_kwargs = client._provider.create_message.call_args[1]
        assert call_kwargs["system"] == f"Role: {config['role']}"
        # No trailing newlines when instructions are empty
        assert not call_kwargs["system"].endswith("\n")

    async def test_llm_error_returns_failed(self):
        client = _make_client()
        client._provider.create_message = AsyncMock(
            side_effect=RuntimeError("API error")
        )
        config = {**_DEFAULT_CONFIG, "instructions": "Write code."}
        agent = TaskAgent(agent_id="code-writer", config=config, llm_client=client)
        output = await agent.handle(_task())
        assert output.status == TaskStatus.FAILED

    async def test_capabilities_from_config(self):
        config = {**_DEFAULT_CONFIG, "capabilities": ["code_generation", "code_review"]}
        agent = TaskAgent(agent_id="code-writer", config=config)
        assert agent.capabilities == ["code_generation", "code_review"]

    async def test_capabilities_empty_without_config(self):
        agent = TaskAgent(agent_id="code-writer", config={"model": "test"})
        assert agent.capabilities == []


# ─── Parametrized Role Tests ────────────────────────────────


_CODER_INSTRUCTIONS = """\
You are a code generation agent. Your job is to write clean, well-tested code
based on the specifications you receive."""

_REVIEWER_INSTRUCTIONS = """\
You are a code review agent. Your job is to review code for correctness,
style, and security issues."""

_PLANNER_INSTRUCTIONS = """\
You are a planning agent. Your job is to decompose high-level goals into
actionable step-by-step plans."""


class TestTaskAgentRoles:
    """Verify TaskAgent replaces all three v0.1 agents with config-driven behavior."""

    @pytest.mark.parametrize(
        "agent_id,role,instructions,payload,expected_in_result",
        [
            (
                "code-writer",
                "Writes clean, tested code from specifications",
                _CODER_INSTRUCTIONS,
                "Write a hello world script",
                "code",
            ),
            (
                "code-reviewer",
                "Reviews code for correctness, style, and security",
                _REVIEWER_INSTRUCTIONS,
                "Review this code: def add(a, b): return a + b",
                "approved",
            ),
            (
                "planner",
                "Decomposes high-level goals into actionable step-by-step plans",
                _PLANNER_INSTRUCTIONS,
                "Plan a web application project",
                "steps",
            ),
        ],
    )
    async def test_role_behavior_via_instructions(
        self, agent_id, role, instructions, payload, expected_in_result
    ):
        result_text = {
            "code-writer": "Here is the code:\n```python\nprint('hi')\n```",
            "code-reviewer": '{"approved": true, "issues": [], "summary": "Code looks good."}',
            "planner": '{"steps": [{"id": 1, "description": "Set up project"}], "summary": "Simple plan"}',
        }
        client = _make_client(
            responses=[LLMResponse(text=result_text[agent_id], stop_reason=StopReason.END_TURN, usage=Usage(50, 100))]
        )
        config = {**_DEFAULT_CONFIG, "role": role, "instructions": instructions}
        agent = TaskAgent(agent_id=agent_id, config=config, llm_client=client)
        output = await agent.handle(_task(payload))
        assert output.status == TaskStatus.COMPLETED
        assert expected_in_result in output.result.lower()

    @pytest.mark.parametrize(
        "agent_id,role,instructions",
        [
            ("code-writer", "Writes code", _CODER_INSTRUCTIONS),
            ("code-reviewer", "Reviews code", _REVIEWER_INSTRUCTIONS),
            ("planner", "Plans tasks", _PLANNER_INSTRUCTIONS),
        ],
    )
    async def test_system_prompt_composition(self, agent_id, role, instructions):
        """System prompt starts with 'Role: ...' followed by instructions."""
        config = {**_DEFAULT_CONFIG, "role": role, "instructions": instructions}
        client = _make_client()
        agent = TaskAgent(agent_id=agent_id, config=config, llm_client=client)
        await agent.handle(_task())
        call_kwargs = client._provider.create_message.call_args[1]
        system = call_kwargs["system"]
        assert system.startswith(f"Role: {role}")
        assert instructions in system


# ─── Cross-Agent Tests ──────────────────────────────────────


class TestCrossAgent:
    """Tests that apply to TaskAgent with various configurations."""

    @pytest.mark.parametrize(
        "agent_id",
        ["code-writer", "code-reviewer", "planner"],
    )
    async def test_no_llm_client_returns_failed(self, agent_id):
        agent = TaskAgent(agent_id=agent_id, config=_DEFAULT_CONFIG)
        output = await agent.handle(_task())
        assert output.status == TaskStatus.FAILED
        assert "LLM client not configured" in output.result

    @pytest.mark.parametrize(
        "agent_id",
        ["code-writer", "code-reviewer", "planner"],
    )
    async def test_name_from_config(self, agent_id):
        config = {**_DEFAULT_CONFIG, "name": "Custom Name"}
        agent = TaskAgent(agent_id=agent_id, config=config)
        assert agent.name == "Custom Name"

    @pytest.mark.parametrize(
        "agent_id",
        ["code-writer", "code-reviewer", "planner"],
    )
    async def test_role_from_config(self, agent_id):
        config = {**_DEFAULT_CONFIG, "role": "Custom Role"}
        agent = TaskAgent(agent_id=agent_id, config=config)
        assert agent.role == "Custom Role"

    @pytest.mark.parametrize(
        "agent_id",
        ["code-writer", "code-reviewer", "planner"],
    )
    async def test_token_counting(self, agent_id):
        client = _make_client(
            responses=[LLMResponse(text="done", stop_reason=StopReason.END_TURN, usage=Usage(100, 200))]
        )
        config = {**_DEFAULT_CONFIG, "instructions": "Do your thing."}
        agent = TaskAgent(agent_id=agent_id, config=config, llm_client=client)
        output = await agent.handle(_task())
        assert output.status == TaskStatus.COMPLETED
        assert output.metadata["tokens_used"] == "300"


# ─── Agent Loader Dispatch Tests ────────────────────────────


class TestAgentLoaderDispatch:
    """Test that _resolve_agent_type dispatches on the 'type' field."""

    def test_type_task_returns_task_string(self):
        from agents.server import _resolve_agent_type

        assert _resolve_agent_type({"id": "test", "type": "task"}) == "task"

    def test_type_default_returns_task_string(self):
        """Agents without a type field default to 'task'."""
        from agents.server import _resolve_agent_type

        assert _resolve_agent_type({"id": "test"}) == "task"

    def test_type_persona_returns_persona_string(self):
        from agents.server import _resolve_agent_type

        assert _resolve_agent_type({"id": "test", "type": "persona"}) == "persona"

    def test_unknown_type_raises_system_exit(self):
        from agents.server import _resolve_agent_type

        with pytest.raises(SystemExit, match="Unknown agent type"):
            _resolve_agent_type({"id": "test", "type": "invalid"})
