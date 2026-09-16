"""
Tests for TaskAgent execution limits (RFC 0006 PR 1c).

Moved from ``test_agents.py``, which had reached the 485-line split point.
All tests use mock LLM client — no real API calls.
"""

from agents.base import TaskInput, TaskInputConfig, TaskStatus
from agents.defaults import DEFAULT_MAX_LLM_CALLS, DEFAULT_MAX_TOKENS
from agents.llm_client import LLMResponse, StopReason, ToolCall, Usage
from agents.task_agent import TaskAgent
from agents.tools.registry import ToolResult, tool

from ._task_agent_test_helpers import _make_client

# ─── Execution Limit Validation Tests (RFC 0006 PR 1c) ──────


def _task_with_config(config: TaskInputConfig) -> TaskInput:
    return TaskInput(task_id="t1", workflow_id="w1", payload="do something", config=config)


_LIMIT_CONFIG: dict = {
    "model": "test-model",
    "role": "Limit test role",
}
_LIMIT_TOOL_CONFIG: dict = {**_LIMIT_CONFIG, "tools": ["noop", "noop_explicit"]}


class TestExecutionLimitValidation:
    """Verify RFC 0006 §B: negative limits rejected, zero resolved to defaults."""

    async def test_negative_max_llm_calls_returns_failed(self):
        client = _make_client()
        agent = TaskAgent(agent_id="test-agent", config=_LIMIT_CONFIG, llm_client=client)
        output = await agent.handle(_task_with_config(TaskInputConfig(max_llm_calls=-1)))
        assert output.status == TaskStatus.FAILED
        assert output.result == "Negative execution limits are not allowed"
        assert output.metadata["error_type"] == "permanent"
        assert output.metadata["invalid_fields"] == "max_llm_calls"

    async def test_negative_max_tokens_returns_failed(self):
        client = _make_client()
        agent = TaskAgent(agent_id="test-agent", config=_LIMIT_CONFIG, llm_client=client)
        output = await agent.handle(_task_with_config(TaskInputConfig(max_tokens=-1)))
        assert output.status == TaskStatus.FAILED
        assert output.result == "Negative execution limits are not allowed"
        assert output.metadata["error_type"] == "permanent"
        assert output.metadata["invalid_fields"] == "max_tokens"

    async def test_negative_both_returns_failed(self):
        client = _make_client()
        agent = TaskAgent(agent_id="test-agent", config=_LIMIT_CONFIG, llm_client=client)
        output = await agent.handle(
            _task_with_config(TaskInputConfig(max_llm_calls=-5, max_tokens=-100))
        )
        assert output.status == TaskStatus.FAILED
        assert output.result == "Negative execution limits are not allowed"
        assert output.metadata["error_type"] == "permanent"
        assert output.metadata["invalid_fields"] == "max_llm_calls,max_tokens"

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
        """Positive max_llm_calls caps the iteration count at the configured value.

        Strengthens the original assertion (N-02): drives the loop with
        TOOL_USE responses so exhaustion is observed at exactly the
        configured value, not at the system default.
        """
        ran: list[str] = []

        @tool(name="noop_explicit", description="No-op tool for explicit limit test")
        async def noop_tool() -> ToolResult:
            ran.append("noop_explicit")
            return ToolResult(success=True, data="no-op")

        tool_response = LLMResponse(
            text=None,
            stop_reason=StopReason.TOOL_USE,
            usage=Usage(10, 20),
            tool_calls=[ToolCall(id="tc1", name="noop_explicit", input={})],
        )
        # Provide more responses than the configured limit so exhaustion is
        # driven by max_llm_calls, not by exhausted mock responses.
        responses = [tool_response] * 10
        client = _make_client(responses=responses)
        agent = TaskAgent(agent_id="test-agent", config=_LIMIT_TOOL_CONFIG, llm_client=client)
        output = await agent.handle(_task_with_config(TaskInputConfig(max_llm_calls=3)))
        assert output.status == TaskStatus.FAILED
        assert "Max LLM call iterations exceeded" in output.result
        assert client._provider.create_message.call_count == 3
        assert ran == ["noop_explicit"] * 3

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
        """With max_llm_calls=0 and LLM always returning TOOL_USE, loop runs
        DEFAULT_MAX_LLM_CALLS times."""
        # Register (and list, in _LIMIT_TOOL_CONFIG) a noop tool so the test
        # doesn't depend on _execute_tools refusing an unknown or unlisted tool;
        # `ran` proves the listed tool really runs each round.
        ran: list[str] = []

        @tool(name="noop", description="No-op tool for loop exhaustion test")
        async def noop_tool() -> ToolResult:
            ran.append("noop")
            return ToolResult(success=True, data="no-op")

        tool_response = LLMResponse(
            text=None,
            stop_reason=StopReason.TOOL_USE,
            usage=Usage(10, 20),
            tool_calls=[ToolCall(id="tc1", name="noop", input={})],
        )
        # Provide enough responses for DEFAULT_MAX_LLM_CALLS iterations.
        responses = [tool_response] * DEFAULT_MAX_LLM_CALLS
        client = _make_client(responses=responses)
        agent = TaskAgent(agent_id="test-agent", config=_LIMIT_TOOL_CONFIG, llm_client=client)
        output = await agent.handle(_task_with_config(TaskInputConfig(max_llm_calls=0)))
        assert output.status == TaskStatus.FAILED
        assert "Max LLM call iterations exceeded" in output.result
        assert client._provider.create_message.call_count == DEFAULT_MAX_LLM_CALLS
        assert ran == ["noop"] * DEFAULT_MAX_LLM_CALLS
