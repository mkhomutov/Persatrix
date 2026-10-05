"""Thinking blocks go back unchanged in a tool round (ISSUE-0169 item 2).

A Claude model that thinks returns its reasoning as ``thinking`` (or
``redacted_thinking``) blocks ahead of its tool calls. When the next request
carries the tool results, the assistant turn before them has to carry those
blocks back exactly as they came, signatures and all, in their original
order: dropping them loses the reasoning behind each call and can make the
request fail. The adapter keeps such a turn's blocks on the response and
replays them. A turn with no thinking block is rebuilt from its text and tool
calls as before, so requests to models that do not think are unchanged.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from agents.llm_providers import AnthropicProvider
from agents.llm_types import LLMResponse, LLMToolResult, StopReason, ToolCall


def _provider() -> AnthropicProvider:
    sdk = MagicMock()
    sdk.AsyncAnthropic.return_value = AsyncMock()
    with patch.dict(sys.modules, {"anthropic": sdk}):
        provider = AnthropicProvider(api_key="test-key")
    provider._client = AsyncMock()
    return provider


def _thinking(text: str, signature: str) -> SimpleNamespace:
    return SimpleNamespace(type="thinking", thinking=text, signature=signature)


def _tool_use(call_id: str, name: str, args: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=call_id, name=name, input=args)


async def _respond(provider: AnthropicProvider, *blocks: SimpleNamespace) -> LLMResponse:
    client: Any = provider._client
    client.messages.create = AsyncMock(
        return_value=SimpleNamespace(
            content=list(blocks),
            stop_reason="tool_use",
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        ),
    )
    return await provider.create_message(
        model="claude-sonnet-5-5",
        messages=[{"role": "user", "content": "look it up"}],
        system="",
        tools=[],
        max_tokens=4096,
        temperature=0.7,
    )


_RESULT = [LLMToolResult(tool_call_id="toolu_1", content="found", is_error=False)]


class TestThinkingIsReplayed:
    async def test_a_thinking_turn_goes_back_whole_and_in_order(self) -> None:
        provider = _provider()
        response = await _respond(
            provider,
            _thinking("", "sig-1"),
            SimpleNamespace(type="text", text="Checking."),
            _tool_use("toolu_1", "lookup", {"q": "x"}),
        )
        messages = provider.append_tool_round([], response, _RESULT)
        assert messages[0] == {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "", "signature": "sig-1"},
                {"type": "text", "text": "Checking."},
                {"type": "tool_use", "id": "toolu_1", "name": "lookup", "input": {"q": "x"}},
            ],
        }
        assert messages[1]["content"][0]["tool_use_id"] == "toolu_1"

    async def test_redacted_thinking_goes_back_with_its_data(self) -> None:
        provider = _provider()
        response = await _respond(
            provider,
            SimpleNamespace(type="redacted_thinking", data="opaque"),
            _tool_use("toolu_1", "lookup", {}),
        )
        messages = provider.append_tool_round([], response, _RESULT)
        assert messages[0]["content"] == [
            {"type": "redacted_thinking", "data": "opaque"},
            {"type": "tool_use", "id": "toolu_1", "name": "lookup", "input": {}},
        ]

    async def test_thinking_text_never_becomes_the_reply(self) -> None:
        provider = _provider()
        response = await _respond(
            provider,
            _thinking("private reasoning", "sig-1"),
            _tool_use("toolu_1", "lookup", {}),
        )
        assert response.text is None
        assert response.tool_calls == [ToolCall(id="toolu_1", name="lookup", input={})]
        assert response.stop_reason is StopReason.TOOL_USE


class TestTurnsWithoutThinkingAreRebuiltAsBefore:
    async def test_no_thinking_keeps_no_provider_content(self) -> None:
        provider = _provider()
        response = await _respond(
            provider,
            SimpleNamespace(type="text", text="Checking."),
            _tool_use("toolu_1", "lookup", {"q": "x"}),
        )
        assert response.provider_content is None

    def test_a_response_built_without_provider_content_replays_text_then_tools(self) -> None:
        response = LLMResponse(
            text="Checking.",
            tool_calls=[ToolCall(id="toolu_1", name="lookup", input={"q": "x"})],
            stop_reason=StopReason.TOOL_USE,
        )
        messages = _provider().append_tool_round([], response, _RESULT)
        assert messages[0]["content"] == [
            {"type": "text", "text": "Checking."},
            {"type": "tool_use", "id": "toolu_1", "name": "lookup", "input": {"q": "x"}},
        ]
