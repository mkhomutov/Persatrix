"""
Tests for the multi-provider LLM client.

All tests use mock SDK clients — no real API calls.
"""

import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.llm_client import (
    AnthropicProvider,
    LLMClient,
    LLMResponse,
    LLMToolResult,
    OpenAIProvider,
    StopReason,
    ToolCall,
    Usage,
    create_provider,
    _infer_provider,
)


# ─── Helpers ────────────────────────────────────────────────


def _make_anthropic_provider() -> AnthropicProvider:
    """Create an AnthropicProvider with a mocked SDK client."""
    mock_anthropic = MagicMock()
    mock_client = AsyncMock()
    mock_anthropic.AsyncAnthropic.return_value = mock_client
    with patch.dict(sys.modules, {"anthropic": mock_anthropic}):
        p = AnthropicProvider(api_key="test-key")
    p._client = mock_client
    return p


def _make_openai_provider() -> OpenAIProvider:
    """Create an OpenAIProvider with a mocked SDK client."""
    mock_openai = MagicMock()
    mock_client = AsyncMock()
    mock_openai.AsyncOpenAI.return_value = mock_client
    with patch.dict(sys.modules, {"openai": mock_openai}):
        p = OpenAIProvider(api_key="test-key")
    p._client = mock_client
    return p


# ─── Helpers ────────────────────────────────────────────────


def _anthropic_response(
    content: list | None = None,
    stop_reason: str = "end_turn",
    input_tokens: int = 100,
    output_tokens: int = 50,
):
    """Build a mock Anthropic response object."""
    if content is None:
        content = [SimpleNamespace(type="text", text="Hello from Claude")]
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def _openai_response(
    content: str | None = "Hello from GPT",
    tool_calls: list | None = None,
    finish_reason: str = "stop",
    prompt_tokens: int = 100,
    completion_tokens: int = 50,
):
    """Build a mock OpenAI response object."""
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(message=message, finish_reason=finish_reason)
    usage = SimpleNamespace(
        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
    )
    return SimpleNamespace(choices=[choice], usage=usage)


# ─── AnthropicProvider ──────────────────────────────────────


class TestAnthropicProvider:
    @pytest.fixture
    def provider(self):
        return _make_anthropic_provider()

    async def test_text_response(self, provider):
        provider._client.messages.create = AsyncMock(
            return_value=_anthropic_response()
        )
        resp = await provider.create_message(
            model="claude-sonnet-4-20250514",
            messages=[{"role": "user", "content": "hi"}],
            system="You are helpful",
            tools=[],
            max_tokens=1024,
            temperature=0.3,
        )
        assert resp.text == "Hello from Claude"
        assert resp.stop_reason == StopReason.END_TURN
        assert resp.usage.input_tokens == 100
        assert resp.usage.output_tokens == 50
        assert resp.tool_calls == []

    async def test_tool_use_response(self, provider):
        tool_block = SimpleNamespace(
            type="tool_use",
            id="toolu_123",
            name="file_read",
            input={"path": "main.py"},
        )
        provider._client.messages.create = AsyncMock(
            return_value=_anthropic_response(
                content=[tool_block], stop_reason="tool_use"
            )
        )
        resp = await provider.create_message(
            model="claude-sonnet-4-20250514",
            messages=[],
            system="",
            tools=[{"name": "file_read", "description": "Read file", "input_schema": {}}],
            max_tokens=1024,
            temperature=0.3,
        )
        assert resp.stop_reason == StopReason.TOOL_USE
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0].id == "toolu_123"
        assert resp.tool_calls[0].name == "file_read"
        assert resp.tool_calls[0].input == {"path": "main.py"}

    async def test_mixed_text_and_tool(self, provider):
        content = [
            SimpleNamespace(type="text", text="Let me read that file."),
            SimpleNamespace(
                type="tool_use", id="t1", name="file_read", input={"path": "x.py"}
            ),
        ]
        provider._client.messages.create = AsyncMock(
            return_value=_anthropic_response(content=content, stop_reason="tool_use")
        )
        resp = await provider.create_message(
            model="claude-sonnet-4-20250514",
            messages=[], system="", tools=[], max_tokens=1024, temperature=0.3,
        )
        assert resp.text == "Let me read that file."
        assert len(resp.tool_calls) == 1

    async def test_max_tokens_stop(self, provider):
        provider._client.messages.create = AsyncMock(
            return_value=_anthropic_response(stop_reason="max_tokens")
        )
        resp = await provider.create_message(
            model="claude-sonnet-4-20250514",
            messages=[], system="", tools=[], max_tokens=10, temperature=0.3,
        )
        assert resp.stop_reason == StopReason.MAX_TOKENS

    async def test_unmapped_stop_reason(self, provider):
        provider._client.messages.create = AsyncMock(
            return_value=_anthropic_response(stop_reason="stop_sequence")
        )
        resp = await provider.create_message(
            model="claude-sonnet-4-20250514",
            messages=[], system="", tools=[], max_tokens=1024, temperature=0.3,
        )
        assert resp.stop_reason == StopReason.END_TURN

    def test_format_tool_definitions(self, provider):
        tools = [
            {"name": "file_read", "description": "Read", "parameters": {"type": "object"}},
        ]
        result = provider.format_tool_definitions(tools)
        assert result == [
            {"name": "file_read", "description": "Read", "input_schema": {"type": "object"}},
        ]

    def test_append_tool_round(self, provider):
        messages = [{"role": "user", "content": "read file"}]
        response = LLMResponse(
            text="Reading...",
            tool_calls=[ToolCall(id="t1", name="file_read", input={"path": "x.py"})],
            stop_reason=StopReason.TOOL_USE,
        )
        results = [LLMToolResult(tool_call_id="t1", content="file content", is_error=False)]
        new_msgs = provider.append_tool_round(messages, response, results)

        assert len(new_msgs) == 3
        assert new_msgs[0] == messages[0]
        assert new_msgs[1]["role"] == "assistant"
        assert len(new_msgs[1]["content"]) == 2  # text + tool_use
        assert new_msgs[2]["role"] == "user"
        assert new_msgs[2]["content"][0]["type"] == "tool_result"
        assert new_msgs[2]["content"][0]["tool_use_id"] == "t1"

    def test_append_tool_round_error_result(self, provider):
        response = LLMResponse(
            text=None,
            tool_calls=[ToolCall(id="t1", name="bad_tool", input={})],
            stop_reason=StopReason.TOOL_USE,
        )
        results = [LLMToolResult(tool_call_id="t1", content="Permission denied", is_error=True)]
        new_msgs = provider.append_tool_round([], response, results)

        result_block = new_msgs[1]["content"][0]
        assert result_block["is_error"] is True

    async def test_no_system_prompt(self, provider):
        """Verify system param is omitted when empty."""
        provider._client.messages.create = AsyncMock(
            return_value=_anthropic_response()
        )
        await provider.create_message(
            model="claude-sonnet-4-20250514",
            messages=[], system="", tools=[], max_tokens=1024, temperature=0.3,
        )
        call_kwargs = provider._client.messages.create.call_args[1]
        assert "system" not in call_kwargs


# ─── OpenAIProvider ─────────────────────────────────────────


class TestOpenAIProvider:
    @pytest.fixture
    def provider(self):
        return _make_openai_provider()

    async def test_text_response(self, provider):
        provider._client.chat.completions.create = AsyncMock(
            return_value=_openai_response()
        )
        resp = await provider.create_message(
            model="gpt-4o",
            messages=[{"role": "user", "content": "hi"}],
            system="You are helpful",
            tools=[],
            max_tokens=1024,
            temperature=0.3,
        )
        assert resp.text == "Hello from GPT"
        assert resp.stop_reason == StopReason.END_TURN
        assert resp.usage.input_tokens == 100
        assert resp.usage.output_tokens == 50

    async def test_tool_calls_response(self, provider):
        tc = SimpleNamespace(
            id="call_123",
            function=SimpleNamespace(
                name="shell_exec",
                arguments=json.dumps({"command": "echo hi"}),
            ),
        )
        provider._client.chat.completions.create = AsyncMock(
            return_value=_openai_response(
                content=None, tool_calls=[tc], finish_reason="tool_calls"
            )
        )
        resp = await provider.create_message(
            model="gpt-4o",
            messages=[], system="", tools=[], max_tokens=1024, temperature=0.3,
        )
        assert resp.stop_reason == StopReason.TOOL_USE
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0].id == "call_123"
        assert resp.tool_calls[0].name == "shell_exec"
        assert resp.tool_calls[0].input == {"command": "echo hi"}

    async def test_max_tokens_stop(self, provider):
        provider._client.chat.completions.create = AsyncMock(
            return_value=_openai_response(finish_reason="length")
        )
        resp = await provider.create_message(
            model="gpt-4o",
            messages=[], system="", tools=[], max_tokens=10, temperature=0.3,
        )
        assert resp.stop_reason == StopReason.MAX_TOKENS

    async def test_unmapped_finish_reason(self, provider):
        provider._client.chat.completions.create = AsyncMock(
            return_value=_openai_response(finish_reason="content_filter")
        )
        resp = await provider.create_message(
            model="gpt-4o",
            messages=[], system="", tools=[], max_tokens=1024, temperature=0.3,
        )
        assert resp.stop_reason == StopReason.END_TURN

    async def test_no_usage(self, provider):
        resp_obj = _openai_response()
        resp_obj.usage = None
        provider._client.chat.completions.create = AsyncMock(return_value=resp_obj)
        resp = await provider.create_message(
            model="gpt-4o",
            messages=[], system="", tools=[], max_tokens=1024, temperature=0.3,
        )
        assert resp.usage.input_tokens == 0
        assert resp.usage.output_tokens == 0

    async def test_system_prepended(self, provider):
        provider._client.chat.completions.create = AsyncMock(
            return_value=_openai_response()
        )
        await provider.create_message(
            model="gpt-4o",
            messages=[{"role": "user", "content": "hi"}],
            system="Be helpful",
            tools=[],
            max_tokens=1024,
            temperature=0.3,
        )
        call_kwargs = provider._client.chat.completions.create.call_args[1]
        assert call_kwargs["messages"][0] == {"role": "system", "content": "Be helpful"}
        assert call_kwargs["messages"][1] == {"role": "user", "content": "hi"}

    async def test_base_url_passthrough(self):
        mock_openai = MagicMock()
        mock_client = AsyncMock()
        mock_openai.AsyncOpenAI.return_value = mock_client
        with patch.dict(sys.modules, {"openai": mock_openai}):
            OpenAIProvider(api_key="k", base_url="http://localhost:11434/v1")
        mock_openai.AsyncOpenAI.assert_called_once_with(
            api_key="k", base_url="http://localhost:11434/v1"
        )

    def test_format_tool_definitions(self, provider):
        tools = [
            {"name": "shell_exec", "description": "Run cmd", "parameters": {"type": "object"}},
        ]
        result = provider.format_tool_definitions(tools)
        assert result == [
            {
                "type": "function",
                "function": {
                    "name": "shell_exec",
                    "description": "Run cmd",
                    "parameters": {"type": "object"},
                },
            },
        ]

    def test_append_tool_round(self, provider):
        messages = [{"role": "user", "content": "run echo"}]
        response = LLMResponse(
            text="Running...",
            tool_calls=[ToolCall(id="c1", name="shell_exec", input={"command": "echo"})],
            stop_reason=StopReason.TOOL_USE,
        )
        results = [LLMToolResult(tool_call_id="c1", content="echo output", is_error=False)]
        new_msgs = provider.append_tool_round(messages, response, results)

        assert len(new_msgs) == 3
        assert new_msgs[1]["role"] == "assistant"
        assert new_msgs[1]["tool_calls"][0]["id"] == "c1"
        assert new_msgs[1]["tool_calls"][0]["type"] == "function"
        assert new_msgs[2]["role"] == "tool"
        assert new_msgs[2]["tool_call_id"] == "c1"

    async def test_malformed_tool_call_json(self, provider):
        """review-fix M2: Invalid JSON in tool call arguments → empty input fallback."""
        tc = SimpleNamespace(
            id="call_bad",
            function=SimpleNamespace(
                name="some_tool",
                arguments="not valid json{{{",
            ),
        )
        provider._client.chat.completions.create = AsyncMock(
            return_value=_openai_response(
                content=None, tool_calls=[tc], finish_reason="tool_calls"
            )
        )
        resp = await provider.create_message(
            model="gpt-4o",
            messages=[], system="", tools=[], max_tokens=1024, temperature=0.3,
        )
        assert resp.stop_reason == StopReason.TOOL_USE
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0].name == "some_tool"
        assert resp.tool_calls[0].input == {}


# ─── LLMClient ──────────────────────────────────────────────


class TestLLMClient:
    def test_delegates_format_tool_definitions(self):
        mock_provider = MagicMock()
        mock_provider.format_tool_definitions.return_value = [{"name": "test"}]
        client = LLMClient(mock_provider)
        result = client.format_tool_definitions([{"name": "test", "description": "", "parameters": {}}])
        mock_provider.format_tool_definitions.assert_called_once()
        assert result == [{"name": "test"}]

    async def test_delegates_create_message(self):
        mock_provider = AsyncMock()
        mock_provider.create_message.return_value = LLMResponse(text="ok")
        client = LLMClient(mock_provider)
        resp = await client.create_message(
            model="test", messages=[], system="", tools=[], max_tokens=100, temperature=0.0,
        )
        assert resp.text == "ok"

    def test_delegates_append_tool_round(self):
        mock_provider = MagicMock()
        mock_provider.append_tool_round.return_value = [{"role": "user"}]
        client = LLMClient(mock_provider)
        result = client.append_tool_round([], LLMResponse(text=None), [])
        mock_provider.append_tool_round.assert_called_once()
        assert result == [{"role": "user"}]


# ─── Provider Factory ────────────────────────────────────────


class TestCreateProvider:
    def test_infer_anthropic_from_model(self):
        assert _infer_provider("claude-sonnet-4-20250514") == "anthropic"
        assert _infer_provider("claude-3-haiku-20240307") == "anthropic"

    def test_infer_openai_from_unknown_model(self):
        assert _infer_provider("gpt-4o") == "openai"
        assert _infer_provider("qwen2.5-coder") == "openai"

    def test_infer_openai_o_series(self):
        """S-10: o-series model prefixes should resolve to openai."""
        assert _infer_provider("o1") == "openai"
        assert _infer_provider("o1-preview") == "openai"
        assert _infer_provider("o1-mini") == "openai"
        assert _infer_provider("o3") == "openai"
        assert _infer_provider("o3-mini") == "openai"
        assert _infer_provider("o4-mini") == "openai"
        assert _infer_provider("o4") == "openai"

    def test_explicit_anthropic_provider(self):
        mock_anthropic = MagicMock()
        mock_anthropic.AsyncAnthropic.return_value = AsyncMock()
        with patch.dict(sys.modules, {"anthropic": mock_anthropic}):
            provider = create_provider({"model": "claude-sonnet-4-20250514", "provider": "anthropic"})
            assert isinstance(provider, AnthropicProvider)

    def test_explicit_openai_provider(self):
        mock_openai = MagicMock()
        mock_openai.AsyncOpenAI.return_value = AsyncMock()
        with patch.dict(sys.modules, {"openai": mock_openai}):
            provider = create_provider({"model": "gpt-4o", "provider": "openai"})
            assert isinstance(provider, OpenAIProvider)

    def test_openai_with_base_url(self):
        mock_openai = MagicMock()
        mock_openai.AsyncOpenAI.return_value = AsyncMock()
        with patch.dict(sys.modules, {"openai": mock_openai}):
            provider = create_provider({
                "model": "qwen2.5-coder:32b",
                "provider": "openai",
                "provider_config": {"base_url": "http://localhost:11434/v1"},
            })
            assert isinstance(provider, OpenAIProvider)

    def test_unknown_provider_exits(self):
        with pytest.raises(SystemExit, match="Unknown LLM provider"):
            create_provider({"model": "test", "provider": "unknown"})

    def test_inferred_from_model_prefix(self):
        mock_anthropic = MagicMock()
        mock_anthropic.AsyncAnthropic.return_value = AsyncMock()
        with patch.dict(sys.modules, {"anthropic": mock_anthropic}):
            provider = create_provider({"model": "claude-sonnet-4-20250514"})
            assert isinstance(provider, AnthropicProvider)
