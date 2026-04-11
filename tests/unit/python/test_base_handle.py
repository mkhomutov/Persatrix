"""
Tests for BaseAgent shared LLM loop (_run_llm_loop, _execute_tools).

All tests use mock LLM client — no real API calls.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.base import (
    BaseAgent,
    TaskInput,
    TaskInputConfig,
    TaskOutput,
    TaskStatus,
)
from agents.llm_client import (
    LLMClient,
    LLMResponse,
    StopReason,
    ToolCall,
    Usage,
)
from agents.tools.registry import ToolResult, clear_registry, tool


# ─── Concrete subclass for testing ──────────────────────────


class _TestableAgent(BaseAgent):
    """Minimal agent that delegates to _run_llm_loop."""

    async def handle(self, task: TaskInput) -> TaskOutput:
        return await self._run_llm_loop(task, system_prompt="You are a test agent.")


# ─── Fixtures ───────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


def _make_agent(
    responses: list[LLMResponse] | None = None,
    config: dict | None = None,
) -> _TestableAgent:
    """Create a _TestableAgent with a mock LLM client."""
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
    client = LLMClient(mock_provider)
    agent_config = config or {"model": "test-model", "max_llm_calls": 10, "max_tokens": 4096}
    return _TestableAgent(agent_id="test-agent", config=agent_config, llm_client=client)


def _task(payload: str = "do something", config: TaskInputConfig | None = None) -> TaskInput:
    return TaskInput(
        task_id="t1",
        workflow_id="w1",
        payload=payload,
        config=config or TaskInputConfig(),
    )


# ─── Handle Loop Tests ──────────────────────────────────────


class TestRunLlmLoopEndTurn:
    async def test_simple_text_response(self):
        agent = _make_agent(
            responses=[LLMResponse(text="Hello!", stop_reason=StopReason.END_TURN, usage=Usage(10, 20))]
        )
        output = await agent.handle(_task())
        assert output.status == TaskStatus.COMPLETED
        assert output.result == "Hello!"
        assert output.metadata["tokens_used"] == "30"
        assert output.metadata["tool_calls"] == "0"

    async def test_empty_text_response(self):
        agent = _make_agent(
            responses=[LLMResponse(text=None, stop_reason=StopReason.END_TURN)]
        )
        output = await agent.handle(_task())
        assert output.status == TaskStatus.COMPLETED
        assert output.result == ""


class TestRunLlmLoopMaxTokens:
    async def test_max_tokens_returns_failed(self):
        agent = _make_agent(
            responses=[LLMResponse(text="truncated...", stop_reason=StopReason.MAX_TOKENS, usage=Usage(50, 50))]
        )
        output = await agent.handle(_task())
        assert output.status == TaskStatus.FAILED
        assert "max_tokens" in output.result
        assert output.metadata["tokens_used"] == "100"


class TestRunLlmLoopToolUse:
    async def test_tool_then_end_turn(self):
        @tool(name="echo_tool", description="Echo input")
        async def echo_tool(text: str) -> ToolResult:
            return ToolResult(success=True, data=f"echoed: {text}")

        tool_call = ToolCall(id="tc1", name="echo_tool", input={"text": "hello"})
        responses = [
            LLMResponse(
                text=None,
                tool_calls=[tool_call],
                stop_reason=StopReason.TOOL_USE,
                usage=Usage(10, 10),
            ),
            LLMResponse(
                text="Done! I echoed your message.",
                stop_reason=StopReason.END_TURN,
                usage=Usage(20, 15),
            ),
        ]
        agent = _make_agent(responses=responses)
        output = await agent.handle(_task())
        assert output.status == TaskStatus.COMPLETED
        assert output.result == "Done! I echoed your message."
        assert output.metadata["tokens_used"] == "55"
        assert output.metadata["tool_calls"] == "1"

    async def test_unknown_tool(self):
        tool_call = ToolCall(id="tc1", name="nonexistent_tool", input={})
        responses = [
            LLMResponse(
                text=None,
                tool_calls=[tool_call],
                stop_reason=StopReason.TOOL_USE,
                usage=Usage(10, 10),
            ),
            LLMResponse(
                text="I couldn't find that tool.",
                stop_reason=StopReason.END_TURN,
                usage=Usage(20, 15),
            ),
        ]
        agent = _make_agent(responses=responses)
        output = await agent.handle(_task())
        assert output.status == TaskStatus.COMPLETED

    async def test_tool_permission_error(self):
        @tool(name="restricted_tool", description="Restricted")
        async def restricted_tool() -> ToolResult:
            raise PermissionError("Access denied")

        tool_call = ToolCall(id="tc1", name="restricted_tool", input={})
        responses = [
            LLMResponse(text=None, tool_calls=[tool_call], stop_reason=StopReason.TOOL_USE, usage=Usage(5, 5)),
            LLMResponse(text="Permission denied.", stop_reason=StopReason.END_TURN, usage=Usage(10, 5)),
        ]
        agent = _make_agent(responses=responses)
        output = await agent.handle(_task())
        assert output.status == TaskStatus.COMPLETED

    async def test_tool_generic_exception(self):
        @tool(name="failing_tool", description="Fails")
        async def failing_tool() -> ToolResult:
            raise RuntimeError("Something broke")

        tool_call = ToolCall(id="tc1", name="failing_tool", input={})
        responses = [
            LLMResponse(text=None, tool_calls=[tool_call], stop_reason=StopReason.TOOL_USE, usage=Usage(5, 5)),
            LLMResponse(text="Tool failed.", stop_reason=StopReason.END_TURN, usage=Usage(10, 5)),
        ]
        agent = _make_agent(responses=responses)
        output = await agent.handle(_task())
        assert output.status == TaskStatus.COMPLETED


class TestRunLlmLoopMaxIterations:
    async def test_exceeds_max_iterations(self):
        tool_call = ToolCall(id="tc1", name="some_tool", input={})
        # Always returns TOOL_USE — will hit max iterations
        responses = [
            LLMResponse(text=None, tool_calls=[tool_call], stop_reason=StopReason.TOOL_USE, usage=Usage(5, 5))
            for _ in range(15)
        ]
        agent = _make_agent(
            responses=responses,
            config={"model": "test", "max_llm_calls": 3, "max_tokens": 4096},
        )
        output = await agent.handle(_task())
        assert output.status == TaskStatus.FAILED
        assert "Max LLM call iterations exceeded" in output.result


class TestTaskConfigOverride:
    async def test_per_task_max_llm_calls(self):
        tool_call = ToolCall(id="tc1", name="x", input={})
        responses = [
            LLMResponse(text=None, tool_calls=[tool_call], stop_reason=StopReason.TOOL_USE, usage=Usage(5, 5))
            for _ in range(10)
        ]
        agent = _make_agent(
            responses=responses,
            config={"model": "test", "max_llm_calls": 10, "max_tokens": 4096},
        )
        # Task config overrides: only 2 iterations allowed
        output = await agent.handle(
            _task(config=TaskInputConfig(max_llm_calls=2))
        )
        assert output.status == TaskStatus.FAILED
        assert "Max LLM call iterations exceeded" in output.result

    async def test_per_task_max_tokens(self):
        agent = _make_agent(
            responses=[LLMResponse(text="ok", stop_reason=StopReason.END_TURN, usage=Usage(5, 5))]
        )
        await agent.handle(_task(config=TaskInputConfig(max_tokens=256)))
        # Verify max_tokens was passed to create_message
        call_kwargs = agent._llm_client._provider.create_message.call_args[1]
        assert call_kwargs["max_tokens"] == 256


class TestNoLlmClient:
    async def test_no_client_returns_failed(self):
        agent = _TestableAgent(agent_id="no-llm", config={"model": "x"})
        output = await agent.handle(_task())
        assert output.status == TaskStatus.FAILED
        assert "LLM client not configured" in output.result


class TestMissingModelConfig:
    """S-08: Fail fast when 'model' key absent from agent config."""

    async def test_missing_model_returns_failed(self):
        agent = _make_agent(config={"max_llm_calls": 10})
        output = await agent.handle(_task())
        assert output.status == TaskStatus.FAILED
        assert "missing required 'model' field" in output.result


class TestLlmProviderError:
    """review-fix S1: Provider SDK exceptions return FAILED instead of raising."""

    async def test_provider_exception_returns_failed(self):
        agent = _make_agent(
            responses=[LLMResponse(text="ok", stop_reason=StopReason.END_TURN)],
        )
        agent._llm_client._provider.create_message = AsyncMock(
            side_effect=RuntimeError("rate limited"),
        )
        output = await agent.handle(_task())
        assert output.status == TaskStatus.FAILED
        assert "LLM provider error" in output.result
        # S-11: raw exception message should NOT leak into result
        assert "rate limited" not in output.result

    async def test_provider_error_preserves_partial_tokens(self):
        @tool(name="err_tool", description="Tool for error test")
        async def err_tool() -> ToolResult:
            return ToolResult(success=True, data="ok")

        tool_call = ToolCall(id="tc1", name="err_tool", input={})
        first_response = LLMResponse(
            text=None, tool_calls=[tool_call],
            stop_reason=StopReason.TOOL_USE, usage=Usage(50, 25),
        )
        agent = _make_agent(responses=[first_response])
        # Second call fails
        agent._llm_client._provider.create_message = AsyncMock(
            side_effect=[first_response, ConnectionError("network down")],
        )
        output = await agent.handle(_task())
        assert output.status == TaskStatus.FAILED
        # S-11: raw exception message should NOT leak into result
        assert output.result == "LLM provider error"
        # Partial token count from first successful round
        assert int(output.metadata["tokens_used"]) == 75


class TestTokenAccumulation:
    async def test_tokens_accumulate_across_rounds(self):
        @tool(name="acc_tool", description="Accumulation tool")
        async def acc_tool() -> ToolResult:
            return ToolResult(success=True, data="ok")

        tool_call = ToolCall(id="tc1", name="acc_tool", input={})
        responses = [
            LLMResponse(text=None, tool_calls=[tool_call], stop_reason=StopReason.TOOL_USE, usage=Usage(100, 50)),
            LLMResponse(text=None, tool_calls=[tool_call], stop_reason=StopReason.TOOL_USE, usage=Usage(200, 100)),
            LLMResponse(text="done", stop_reason=StopReason.END_TURN, usage=Usage(300, 150)),
        ]
        agent = _make_agent(responses=responses)
        output = await agent.handle(_task())
        assert output.status == TaskStatus.COMPLETED
        # 150 + 300 + 450 = 900
        assert output.metadata["tokens_used"] == "900"
        assert output.metadata["tool_calls"] == "2"


# ─── _execute_tools Tests ───────────────────────────────────


class TestExecuteTools:
    async def test_successful_tool(self):
        @tool(name="good_tool", description="Works")
        async def good_tool(x: str) -> ToolResult:
            return ToolResult(success=True, data=f"result: {x}")

        agent = _TestableAgent(agent_id="t", config={})
        results = await agent._execute_tools(
            [ToolCall(id="c1", name="good_tool", input={"x": "hello"})]
        )
        assert len(results) == 1
        assert results[0].content == "result: hello"
        assert results[0].is_error is False

    async def test_failed_tool(self):
        @tool(name="bad_tool", description="Error")
        async def bad_tool() -> ToolResult:
            return ToolResult(success=False, error="something broke")

        agent = _TestableAgent(agent_id="t", config={})
        results = await agent._execute_tools(
            [ToolCall(id="c1", name="bad_tool", input={})]
        )
        assert results[0].content == "something broke"
        assert results[0].is_error is True

    async def test_unknown_tool(self):
        agent = _TestableAgent(agent_id="t", config={})
        results = await agent._execute_tools(
            [ToolCall(id="c1", name="nonexistent", input={})]
        )
        assert results[0].is_error is True
        assert "Unknown tool" in results[0].content

    async def test_permission_error_caught(self):
        @tool(name="perm_tool", description="Perm check")
        async def perm_tool() -> ToolResult:
            raise PermissionError("denied")

        agent = _TestableAgent(agent_id="t", config={})
        results = await agent._execute_tools(
            [ToolCall(id="c1", name="perm_tool", input={})]
        )
        assert results[0].is_error is True
        assert "denied" in results[0].content

    async def test_generic_exception_caught(self):
        @tool(name="crash_tool", description="Crashes")
        async def crash_tool() -> ToolResult:
            raise ValueError("boom")

        agent = _TestableAgent(agent_id="t", config={})
        results = await agent._execute_tools(
            [ToolCall(id="c1", name="crash_tool", input={})]
        )
        assert results[0].is_error is True
        assert "ValueError" in results[0].content
        assert "boom" in results[0].content

    async def test_multiple_tools_sequential(self):
        call_order: list[str] = []

        @tool(name="tool_a", description="A")
        async def tool_a() -> ToolResult:
            call_order.append("a")
            return ToolResult(success=True, data="a")

        @tool(name="tool_b", description="B")
        async def tool_b() -> ToolResult:
            call_order.append("b")
            return ToolResult(success=True, data="b")

        agent = _TestableAgent(agent_id="t", config={})
        results = await agent._execute_tools([
            ToolCall(id="c1", name="tool_a", input={}),
            ToolCall(id="c2", name="tool_b", input={}),
        ])
        assert len(results) == 2
        assert call_order == ["a", "b"]


# ─── Capabilities Property ──────────────────────────────────


class TestCapabilities:
    def test_config_driven(self):
        agent = _TestableAgent(
            agent_id="t",
            config={"capabilities": ["code_generation", "testing"]},
        )
        assert agent.capabilities == ["code_generation", "testing"]

    def test_default_empty(self):
        agent = _TestableAgent(agent_id="t", config={})
        assert agent.capabilities == []


# ─── Build Tool Definitions ─────────────────────────────────


class TestBuildToolDefinitions:
    def test_builds_from_registry(self):
        @tool(name="my_tool", description="My tool")
        async def my_tool(path: str) -> ToolResult:
            return ToolResult(success=True, data="ok")

        # S-12: agent must have the tool in its config to expose it
        agent = _TestableAgent(agent_id="t", config={"tools": ["my_tool"]})
        defs = agent._build_tool_definitions()
        assert len(defs) == 1
        assert defs[0]["name"] == "my_tool"
        assert defs[0]["description"] == "My tool"
        assert "path" in defs[0]["parameters"]

    def test_empty_registry(self):
        agent = _TestableAgent(agent_id="t", config={})
        defs = agent._build_tool_definitions()
        assert defs == []

    def test_empty_tools_config_exposes_nothing(self):
        """S-12: empty tools list means no tools exposed."""
        @tool(name="hidden_tool", description="Should not appear")
        async def hidden_tool() -> ToolResult:
            return ToolResult(success=True, data="ok")

        agent = _TestableAgent(agent_id="t", config={"tools": []})
        defs = agent._build_tool_definitions()
        assert defs == []
