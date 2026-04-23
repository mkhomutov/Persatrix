"""Concrete :class:`LLMProvider` implementations for Anthropic and OpenAI.

Extracted from :mod:`agents.llm_client` to keep that module within the
review-friendly 500-line cap. The public types (``StopReason``,
``LLMResponse``, ``LLMProvider`` Protocol, …) still live in
``agents.llm_client`` and are imported here.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .llm_types import (
    LLMResponse,
    LLMToolResult,
    StopReason,
    ToolCall,
    Usage,
)

logger = logging.getLogger(__name__)


# ─── Anthropic Provider ─────────────────────────────────────


_ANTHROPIC_STOP_MAP: dict[str, StopReason] = {
    "end_turn": StopReason.END_TURN,
    "tool_use": StopReason.TOOL_USE,
    "max_tokens": StopReason.MAX_TOKENS,
}


class AnthropicProvider:
    """Wraps anthropic.AsyncAnthropic, translates to LLMResponse."""

    name = "anthropic"

    def __init__(self, api_key: str | None = None):
        import anthropic

        self._client = anthropic.AsyncAnthropic(api_key=api_key)

    async def create_message(
        self,
        *,
        model: str,
        messages: list,
        system: str,
        tools: list,
        max_tokens: int,
        temperature: float,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = tools
        response = await self._client.messages.create(**kwargs)
        return self._normalize(response)

    def _normalize(self, response: Any) -> LLMResponse:
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []

        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(
                    ToolCall(id=block.id, name=block.name, input=block.input)
                )

        stop_reason = _ANTHROPIC_STOP_MAP.get(response.stop_reason)
        if stop_reason is None:
            logger.warning(
                "Unmapped Anthropic stop_reason %r, defaulting to END_TURN",
                response.stop_reason,
            )
            stop_reason = StopReason.END_TURN

        return LLMResponse(
            text="\n".join(text_parts) if text_parts else None,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            usage=Usage(
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            ),
        )

    def format_tool_definitions(self, tools: list[dict]) -> list[dict]:
        return [
            {
                "name": t["name"],
                "description": t["description"],
                "input_schema": t["parameters"],
            }
            for t in tools
        ]

    def append_tool_round(
        self,
        messages: list,
        response: LLMResponse,
        tool_results: list[LLMToolResult],
    ) -> list:
        # Build assistant content blocks from the response
        assistant_content: list[dict[str, Any]] = []
        if response.text:
            assistant_content.append({"type": "text", "text": response.text})
        for tc in response.tool_calls:
            assistant_content.append(
                {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.input}
            )

        # Build user message with tool_result blocks
        result_blocks: list[dict[str, Any]] = []
        for tr in tool_results:
            block: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": tr.tool_call_id,
                "content": tr.content,
            }
            if tr.is_error:
                block["is_error"] = True
            result_blocks.append(block)

        return [
            *messages,
            {"role": "assistant", "content": assistant_content},
            {"role": "user", "content": result_blocks},
        ]


# ─── OpenAI Provider ────────────────────────────────────────


_OPENAI_STOP_MAP: dict[str | None, StopReason] = {
    "stop": StopReason.END_TURN,
    "tool_calls": StopReason.TOOL_USE,
    "length": StopReason.MAX_TOKENS,
}


class OpenAIProvider:
    """Wraps openai.AsyncOpenAI, translates to LLMResponse.

    Also supports any OpenAI-compatible API (Ollama, vLLM, Together, Groq,
    LM Studio) via base_url override.
    """

    name = "openai"

    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        import openai

        kwargs: dict[str, Any] = {}
        if api_key:
            kwargs["api_key"] = api_key
        if base_url:
            kwargs["base_url"] = base_url
        self._client = openai.AsyncOpenAI(**kwargs)

    async def create_message(
        self,
        *,
        model: str,
        messages: list,
        system: str,
        tools: list,
        max_tokens: int,
        temperature: float,
    ) -> LLMResponse:
        oai_messages: list[dict[str, Any]] = []
        if system:
            oai_messages.append({"role": "system", "content": system})
        oai_messages.extend(messages)

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": oai_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            kwargs["tools"] = tools
        response = await self._client.chat.completions.create(**kwargs)
        return self._normalize(response)

    def _normalize(self, response: Any) -> LLMResponse:
        choice = response.choices[0]
        message = choice.message

        tool_calls: list[ToolCall] = []
        if message.tool_calls:
            for tc in message.tool_calls:
                # review-fix M2: OpenAI occasionally returns invalid JSON in
                # function.arguments (especially with complex schemas).
                # Fallback to empty dict keeps the agent loop running.
                try:
                    input_args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    logger.warning(
                        "Invalid JSON in tool call arguments for %s, "
                        "falling back to empty input",
                        tc.function.name,
                    )
                    input_args = {}
                tool_calls.append(
                    ToolCall(
                        id=tc.id,
                        name=tc.function.name,
                        input=input_args,
                    )
                )

        stop_reason = _OPENAI_STOP_MAP.get(choice.finish_reason)
        if stop_reason is None:
            logger.warning(
                "Unmapped OpenAI finish_reason %r, defaulting to END_TURN",
                choice.finish_reason,
            )
            stop_reason = StopReason.END_TURN

        usage = Usage(0, 0)
        if response.usage:
            usage = Usage(
                input_tokens=response.usage.prompt_tokens,
                output_tokens=response.usage.completion_tokens,
            )

        return LLMResponse(
            text=message.content,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            usage=usage,
        )

    def format_tool_definitions(self, tools: list[dict]) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["parameters"],
                },
            }
            for t in tools
        ]

    def append_tool_round(
        self,
        messages: list,
        response: LLMResponse,
        tool_results: list[LLMToolResult],
    ) -> list:
        # Build assistant message with tool_calls
        oai_tool_calls = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": json.dumps(tc.input)},
            }
            for tc in response.tool_calls
        ]
        assistant_msg: dict[str, Any] = {
            "role": "assistant",
            "content": response.text or "",
            "tool_calls": oai_tool_calls,
        }

        # Build tool-role messages (one per result)
        tool_msgs = [
            {"role": "tool", "tool_call_id": tr.tool_call_id, "content": tr.content}
            for tr in tool_results
        ]

        return [*messages, assistant_msg, *tool_msgs]
