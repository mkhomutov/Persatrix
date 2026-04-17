"""
Tests for TaskAgent — backward compatibility with v0.1 agent behavior.

Verifies that TaskAgent with appropriate instructions produces the same
behavior as the removed CoderAgent, ReviewerAgent, and PlannerAgent.
All tests use mock LLM client — no real API calls.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.base import TaskInput, TaskInputConfig, TaskStatus
from agents.defaults import DEFAULT_MAX_LLM_CALLS, DEFAULT_MAX_TOKENS
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


# ─── Coder-role TaskAgent Tests ─────────────────────────────

_CODER_INSTRUCTIONS = """\
You are a code generation agent. Your job is to write clean, well-tested code
based on the specifications you receive."""

_CODER_CONFIG: dict = {
    **_DEFAULT_CONFIG,
    "instructions": _CODER_INSTRUCTIONS,
}


class TestCoderAgent:
    async def test_handle_returns_completed(self):
        client = _make_client(
            responses=[LLMResponse(text="Here is the code:\n```python\nprint('hi')\n```", stop_reason=StopReason.END_TURN, usage=Usage(50, 100))]
        )
        agent = TaskAgent(agent_id="code-writer", config=_CODER_CONFIG, llm_client=client)
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
        config = {**_CODER_CONFIG, "tools": ["file_write"]}
        client = _make_client(responses=responses)
        agent = TaskAgent(agent_id="code-writer", config=config, llm_client=client)
        output = await agent.handle(_task("Write a hello world script"))
        assert output.status == TaskStatus.COMPLETED
        assert output.metadata["tool_calls"] == "1"

    async def test_system_prompt_includes_role(self):
        config = {**_CODER_CONFIG, "role": "Senior Python developer"}
        client = _make_client()
        agent = TaskAgent(agent_id="code-writer", config=config, llm_client=client)
        await agent.handle(_task())
        call_kwargs = client._provider.create_message.call_args[1]
        assert "Senior Python developer" in call_kwargs["system"]

    async def test_llm_error_returns_failed(self):
        client = _make_client()
        client._provider.create_message = AsyncMock(
            side_effect=RuntimeError("API error")
        )
        agent = TaskAgent(agent_id="code-writer", config=_CODER_CONFIG, llm_client=client)
        output = await agent.handle(_task())
        assert output.status == TaskStatus.FAILED

    async def test_capabilities_from_config(self):
        config = {**_CODER_CONFIG, "capabilities": ["code_generation", "code_review"]}
        agent = TaskAgent(agent_id="code-writer", config=config)
        assert agent.capabilities == ["code_generation", "code_review"]

    async def test_capabilities_empty_without_config(self):
        agent = TaskAgent(agent_id="code-writer", config={"model": "test"})
        assert agent.capabilities == []


# ─── Reviewer-role TaskAgent Tests ──────────────────────────

_REVIEWER_INSTRUCTIONS = """\
You are a code review agent. Your job is to review code for correctness,
style, and security issues."""

_REVIEWER_CONFIG: dict = {
    **_DEFAULT_CONFIG,
    "instructions": _REVIEWER_INSTRUCTIONS,
}


class TestReviewerAgent:
    async def test_handle_returns_completed(self):
        review_output = '{"approved": true, "issues": [], "summary": "Code looks good."}'
        client = _make_client(
            responses=[LLMResponse(text=review_output, stop_reason=StopReason.END_TURN, usage=Usage(80, 60))]
        )
        agent = TaskAgent(agent_id="code-reviewer", config=_REVIEWER_CONFIG, llm_client=client)
        output = await agent.handle(_task("Review this code: def add(a, b): return a + b"))
        assert output.status == TaskStatus.COMPLETED
        assert "approved" in output.result

    async def test_system_prompt_includes_role(self):
        config = {**_REVIEWER_CONFIG, "role": "Security-focused code reviewer"}
        client = _make_client()
        agent = TaskAgent(agent_id="code-reviewer", config=config, llm_client=client)
        await agent.handle(_task())
        call_kwargs = client._provider.create_message.call_args[1]
        assert "Security-focused code reviewer" in call_kwargs["system"]

    async def test_llm_error_returns_failed(self):
        client = _make_client()
        client._provider.create_message = AsyncMock(
            side_effect=ConnectionError("timeout")
        )
        agent = TaskAgent(agent_id="code-reviewer", config=_REVIEWER_CONFIG, llm_client=client)
        output = await agent.handle(_task())
        assert output.status == TaskStatus.FAILED

    async def test_capabilities_from_config(self):
        config = {**_REVIEWER_CONFIG, "capabilities": ["code_review", "security_audit"]}
        agent = TaskAgent(agent_id="code-reviewer", config=config)
        assert agent.capabilities == ["code_review", "security_audit"]

    async def test_handle_with_tool_use(self):
        """S-13: Reviewer tool-use test (parity with coder)."""

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
        config = {**_REVIEWER_CONFIG, "tools": ["file_read"]}
        client = _make_client(responses=responses)
        agent = TaskAgent(
            agent_id="code-reviewer",
            config=config,
            llm_client=client,
        )
        output = await agent.handle(_task("Review main.py"))
        assert output.status == TaskStatus.COMPLETED
        assert output.metadata["tool_calls"] == "1"


# ─── Planner-role TaskAgent Tests ───────────────────────────

_PLANNER_INSTRUCTIONS = """\
You are a planning agent. Your job is to decompose high-level goals into
actionable step-by-step plans."""

_PLANNER_CONFIG: dict = {
    **_DEFAULT_CONFIG,
    "instructions": _PLANNER_INSTRUCTIONS,
}


class TestPlannerAgent:
    async def test_handle_returns_completed(self):
        plan_output = '{"steps": [{"id": 1, "description": "Set up project", "depends_on": [], "effort": "small"}], "summary": "Simple setup plan"}'
        client = _make_client(
            responses=[LLMResponse(text=plan_output, stop_reason=StopReason.END_TURN, usage=Usage(60, 80))]
        )
        agent = TaskAgent(agent_id="planner", config=_PLANNER_CONFIG, llm_client=client)
        output = await agent.handle(_task("Plan a web application project"))
        assert output.status == TaskStatus.COMPLETED
        assert "steps" in output.result

    async def test_system_prompt_includes_role(self):
        config = {**_PLANNER_CONFIG, "role": "Technical project planner"}
        client = _make_client()
        agent = TaskAgent(agent_id="planner", config=config, llm_client=client)
        await agent.handle(_task())
        call_kwargs = client._provider.create_message.call_args[1]
        assert "Technical project planner" in call_kwargs["system"]

    async def test_llm_error_returns_failed(self):
        client = _make_client()
        client._provider.create_message = AsyncMock(
            side_effect=RuntimeError("rate limited")
        )
        agent = TaskAgent(agent_id="planner", config=_PLANNER_CONFIG, llm_client=client)
        output = await agent.handle(_task())
        assert output.status == TaskStatus.FAILED

    async def test_capabilities_from_config(self):
        config = {**_PLANNER_CONFIG, "capabilities": ["planning", "decomposition"]}
        agent = TaskAgent(agent_id="planner", config=config)
        assert agent.capabilities == ["planning", "decomposition"]


# ─── Cross-Agent Tests ──────────────────────────────────────


class TestCrossAgent:
    """Tests that apply to TaskAgent with all three role configurations."""

    @pytest.mark.parametrize(
        "agent_id,config",
        [
            ("code-writer", _CODER_CONFIG),
            ("code-reviewer", _REVIEWER_CONFIG),
            ("planner", _PLANNER_CONFIG),
        ],
    )
    async def test_no_llm_client_returns_failed(self, agent_id, config):
        agent = TaskAgent(agent_id=agent_id, config=config)
        output = await agent.handle(_task())
        assert output.status == TaskStatus.FAILED
        assert "LLM client not configured" in output.result

    @pytest.mark.parametrize(
        "agent_id,config",
        [
            ("code-writer", _CODER_CONFIG),
            ("code-reviewer", _REVIEWER_CONFIG),
            ("planner", _PLANNER_CONFIG),
        ],
    )
    async def test_name_from_config(self, agent_id, config):
        cfg = {**config, "name": "Custom Name"}
        agent = TaskAgent(agent_id=agent_id, config=cfg)
        assert agent.name == "Custom Name"

    @pytest.mark.parametrize(
        "agent_id,config",
        [
            ("code-writer", _CODER_CONFIG),
            ("code-reviewer", _REVIEWER_CONFIG),
            ("planner", _PLANNER_CONFIG),
        ],
    )
    async def test_role_from_config(self, agent_id, config):
        cfg = {**config, "role": "Custom Role"}
        agent = TaskAgent(agent_id=agent_id, config=cfg)
        assert agent.role == "Custom Role"

    @pytest.mark.parametrize(
        "agent_id,config",
        [
            ("code-writer", _CODER_CONFIG),
            ("code-reviewer", _REVIEWER_CONFIG),
            ("planner", _PLANNER_CONFIG),
        ],
    )
    async def test_token_counting(self, agent_id, config):
        client = _make_client(
            responses=[LLMResponse(text="done", stop_reason=StopReason.END_TURN, usage=Usage(100, 200))]
        )
        agent = TaskAgent(agent_id=agent_id, config=config, llm_client=client)
        output = await agent.handle(_task())
        assert output.status == TaskStatus.COMPLETED
        assert output.metadata["tokens_used"] == "300"


# ─── Execution Limit Validation Tests (RFC 0006 PR 1c) ──────


def _task_with_config(config: TaskInputConfig) -> TaskInput:
    return TaskInput(task_id="t1", workflow_id="w1", payload="do something", config=config)


_LIMIT_CONFIG: dict = {
    "model": "test-model",
    "role": "Limit test role",
}


class TestExecutionLimitValidation:
    """Verify RFC 0006 §B: negative limits rejected, zero resolved to defaults."""

    async def test_negative_max_llm_calls_raises(self):
        client = _make_client()
        agent = TaskAgent(agent_id="test-agent", config=_LIMIT_CONFIG, llm_client=client)
        output = await agent.handle(_task_with_config(TaskInputConfig(max_llm_calls=-1)))
        assert output.status == TaskStatus.FAILED
        assert output.result == "Negative execution limits are not allowed"
        assert output.metadata["error_type"] == "permanent"

    async def test_negative_max_tokens_raises(self):
        client = _make_client()
        agent = TaskAgent(agent_id="test-agent", config=_LIMIT_CONFIG, llm_client=client)
        output = await agent.handle(_task_with_config(TaskInputConfig(max_tokens=-1)))
        assert output.status == TaskStatus.FAILED
        assert output.result == "Negative execution limits are not allowed"
        assert output.metadata["error_type"] == "permanent"

    async def test_negative_both_raises(self):
        client = _make_client()
        agent = TaskAgent(agent_id="test-agent", config=_LIMIT_CONFIG, llm_client=client)
        output = await agent.handle(
            _task_with_config(TaskInputConfig(max_llm_calls=-5, max_tokens=-100))
        )
        assert output.status == TaskStatus.FAILED
        assert output.result == "Negative execution limits are not allowed"
        assert output.metadata["error_type"] == "permanent"

    async def test_zero_max_llm_calls_resolves_to_default(self):
        """Zero max_llm_calls falls through to DEFAULT_MAX_LLM_CALLS (5)."""
        # Agent config has no max_llm_calls — zero must reach the system default.
        response = LLMResponse(text="done", stop_reason=StopReason.END_TURN, usage=Usage(10, 20))
        client = _make_client(responses=[response])
        agent = TaskAgent(agent_id="test-agent", config=_LIMIT_CONFIG, llm_client=client)
        output = await agent.handle(_task_with_config(TaskInputConfig(max_llm_calls=0)))
        assert output.status == TaskStatus.COMPLETED
        # Exactly one LLM call was made (loop ran, ended on END_TURN).
        assert client._provider.create_message.call_count == 1

    async def test_zero_max_tokens_resolves_to_default(self):
        """Zero max_tokens falls through to DEFAULT_MAX_TOKENS (8192)."""
        response = LLMResponse(text="done", stop_reason=StopReason.END_TURN, usage=Usage(10, 20))
        client = _make_client(responses=[response])
        agent = TaskAgent(agent_id="test-agent", config=_LIMIT_CONFIG, llm_client=client)
        output = await agent.handle(_task_with_config(TaskInputConfig(max_tokens=0)))
        assert output.status == TaskStatus.COMPLETED
        call_kwargs = client._provider.create_message.call_args[1]
        assert call_kwargs["max_tokens"] == DEFAULT_MAX_TOKENS

    async def test_explicit_max_llm_calls_used_as_is(self):
        """Positive max_llm_calls from TaskInputConfig is used without modification."""
        response = LLMResponse(text="done", stop_reason=StopReason.END_TURN, usage=Usage(10, 20))
        client = _make_client(responses=[response])
        agent = TaskAgent(agent_id="test-agent", config=_LIMIT_CONFIG, llm_client=client)
        output = await agent.handle(_task_with_config(TaskInputConfig(max_llm_calls=3)))
        assert output.status == TaskStatus.COMPLETED

    async def test_explicit_max_tokens_used_as_is(self):
        """Positive max_tokens from TaskInputConfig is passed to the LLM call."""
        response = LLMResponse(text="done", stop_reason=StopReason.END_TURN, usage=Usage(10, 20))
        client = _make_client(responses=[response])
        agent = TaskAgent(agent_id="test-agent", config=_LIMIT_CONFIG, llm_client=client)
        output = await agent.handle(_task_with_config(TaskInputConfig(max_tokens=512)))
        assert output.status == TaskStatus.COMPLETED
        call_kwargs = client._provider.create_message.call_args[1]
        assert call_kwargs["max_tokens"] == 512

    async def test_zero_limits_agent_config_overrides_default(self):
        """When TaskInputConfig is zero, agent-level config takes priority over system default."""
        response = LLMResponse(text="done", stop_reason=StopReason.END_TURN, usage=Usage(10, 20))
        client = _make_client(responses=[response])
        config = {**_LIMIT_CONFIG, "max_tokens": 2048}
        agent = TaskAgent(agent_id="test-agent", config=config, llm_client=client)
        output = await agent.handle(_task_with_config(TaskInputConfig(max_tokens=0)))
        assert output.status == TaskStatus.COMPLETED
        call_kwargs = client._provider.create_message.call_args[1]
        # Agent config (2048) should take priority over system default (8192).
        assert call_kwargs["max_tokens"] == 2048

    async def test_loop_exhaustion_uses_default_max_llm_calls(self):
        """With max_llm_calls=0 and LLM always returning TOOL_USE, loop runs DEFAULT_MAX_LLM_CALLS times."""
        tool_response = LLMResponse(
            text=None,
            stop_reason=StopReason.TOOL_USE,
            usage=Usage(10, 20),
            tool_calls=[ToolCall(id="tc1", name="noop", input={})],
        )
        # Provide enough responses for DEFAULT_MAX_LLM_CALLS iterations.
        responses = [tool_response] * DEFAULT_MAX_LLM_CALLS
        client = _make_client(responses=responses)
        agent = TaskAgent(agent_id="test-agent", config=_LIMIT_CONFIG, llm_client=client)
        output = await agent.handle(_task_with_config(TaskInputConfig(max_llm_calls=0)))
        assert output.status == TaskStatus.FAILED
        assert "Max LLM call iterations exceeded" in output.result
        assert client._provider.create_message.call_count == DEFAULT_MAX_LLM_CALLS
