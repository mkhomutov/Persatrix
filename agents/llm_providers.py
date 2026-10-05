"""Concrete :class:`LLMProvider` implementations for Anthropic and OpenAI.

Extracted from :mod:`agents.llm_client` to keep that module within the
review-friendly 500-line cap. The public types (``StopReason``,
``LLMResponse``, ``LLMProvider`` Protocol, …) still live in
``agents.llm_client`` and are imported here.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from .llm_temperature import takes_temperature
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

# Claude models from Opus 4.7 on reject ``temperature`` with an HTTP 400.
# These prefixes name the older families that still accept it; every other
# model is sent none, so a newly released model works without a change here.
_TEMPERATURE_MODEL_PREFIXES: tuple[str, ...] = (
    "claude-3",
    "claude-haiku-4",
    "claude-sonnet-4",
    "claude-opus-4-0",
    "claude-opus-4-1",
    "claude-opus-4-5",
    "claude-opus-4-6",
    "claude-opus-4-2025",
)

# Models already warned about, so a caller's dropped temperature is logged
# once per model rather than on every call.
_warned_no_temperature: set[str] = set()

_CACHE_MARKER: dict[str, str] = {"type": "ephemeral"}


def _content_param(block: Any) -> dict[str, Any] | None:
    """A response content block as the request param that sends it back
    unchanged, or None for a kind a tool round never replays."""
    if block.type == "thinking":
        return {"type": "thinking", "thinking": block.thinking, "signature": block.signature}
    if block.type == "redacted_thinking":
        return {"type": "redacted_thinking", "data": block.data}
    if block.type == "text":
        return {"type": "text", "text": block.text}
    if block.type == "tool_use":
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    return None


class AnthropicProvider:
    """Wraps anthropic.AsyncAnthropic, translates to LLMResponse.

    Three request settings come from an alias's ``provider_config``, and an
    alias that sets none sends a request with none of these fields:

    * ``thinking`` — the thinking type: ``adaptive``, ``disabled``, or (on
      Claude Sonnet 5.5) ``between_tools``.
    * ``effort`` — ``low`` to ``max``: how much the model thinks and writes.
      Claude Sonnet 5.5 defaults to ``high`` and thinks before almost every
      reply from ``medium`` up; ``low`` skips thinking on most simple ones.
    * ``prompt_cache`` — ``true`` writes each request's prompt to Anthropic's
      cache and reads whatever an earlier request already wrote: the last
      tool definition and the system prompt get markers of their own, so
      calls that share them read them, and the whole request is marked too,
      so the next round of a tool loop, which extends it, reads all of it. A
      write costs 1.25 times the input price and a read a tenth of it, so it
      pays when calls share a prefix within five minutes, and costs a quarter
      more on a prompt nothing reuses (ISSUE-0182).
    """

    name = "anthropic"
    # LLMClient hands ``cache_prefix`` only to a provider that says True here.
    supports_prompt_cache = True
    # ...and the calling alias's ``provider_config`` only to one that says so.
    accepts_provider_config = True

    def __init__(
        self,
        api_key: str | None = None,
        provider_config: dict[str, Any] | None = None,
    ):
        import anthropic

        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        # The settings for a call that names no alias (see create_message).
        self._provider_config = dict(provider_config or {})

    async def create_message(
        self,
        *,
        model: str,
        messages: list,
        system: str,
        tools: list,
        max_tokens: int,
        temperature: float,
        cache_prefix: str = "",
        provider_config: dict[str, Any] | None = None,
    ) -> LLMResponse:
        """Send one request. A ``cache_prefix`` goes first in the system
        prompt, marked so Anthropic caches everything up to its end; the
        first call writes the cache and later ones with the same prefix read
        it. Without one, and without ``prompt_cache``, the request carries no
        cache marker at all. ``temperature`` goes only to a model that
        accepts it (see ``_TEMPERATURE_MODEL_PREFIXES``) and is not told to
        think.

        ``provider_config`` is the calling alias's, which ``LLMClient``
        hands over per call; its settings replace the ones this provider was
        built with, whole, so a lane alias that sets none sends none."""
        settings = self._provider_config if provider_config is None else provider_config
        cache = settings.get("prompt_cache") is True
        thinking = settings.get("thinking")
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        # Anthropic takes a temperature only from a model that is not
        # thinking, so an alias that turns thinking on leaves no family that
        # takes one.
        thinks = bool(thinking) and thinking != "disabled"
        if takes_temperature(
            model, () if thinks else _TEMPERATURE_MODEL_PREFIXES,
            asked=temperature, warned=_warned_no_temperature, log=logger,
            reason="Anthropic takes none from a model that is thinking" if thinks else
            "it is not in _TEMPERATURE_MODEL_PREFIXES; add it there if it accepts one",
        ):
            kwargs["temperature"] = temperature
        if thinking:
            kwargs["thinking"] = {"type": thinking}
        if settings.get("effort"):
            kwargs["output_config"] = {"effort": settings["effort"]}
        if cache_prefix or (cache and system):
            blocks: list[dict[str, Any]] = []
            if cache_prefix:
                blocks.append(
                    {"type": "text", "text": cache_prefix, "cache_control": _CACHE_MARKER},
                )
            if system:
                block: dict[str, Any] = {"type": "text", "text": system}
                if cache:
                    block["cache_control"] = _CACHE_MARKER
                blocks.append(block)
            kwargs["system"] = blocks
        elif system:
            kwargs["system"] = system
        if cache:
            # Top-level: the API marks the request's last block, so a later
            # request that extends this one reads all of it.
            kwargs["cache_control"] = _CACHE_MARKER
        if tools and cache:
            # Tools come first in the prompt and stay the same across an
            # agent's calls, so a marker after them is read turn after turn.
            kwargs["tools"] = [*tools[:-1], {**tools[-1], "cache_control": _CACHE_MARKER}]
        elif tools:
            kwargs["tools"] = tools
        response = await self._client.messages.create(**kwargs)
        return self._normalize(response)

    def _normalize(self, response: Any) -> LLMResponse:
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        content: list[dict[str, Any]] = []
        thought = False

        for block in response.content:
            param = _content_param(block)
            if param is not None:
                content.append(param)
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(
                    ToolCall(id=block.id, name=block.name, input=block.input)
                )
            elif block.type in ("thinking", "redacted_thinking"):
                thought = True

        stop_reason = _ANTHROPIC_STOP_MAP.get(response.stop_reason)
        if stop_reason is None:
            logger.warning(
                "Unmapped Anthropic stop_reason %r, defaulting to END_TURN",
                response.stop_reason,
            )
            stop_reason = StopReason.END_TURN

        usage = response.usage
        return LLMResponse(
            text="\n".join(text_parts) if text_parts else None,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            usage=Usage(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                # Absent on older SDKs and null when nothing was cached.
                cache_write_tokens=getattr(usage, "cache_creation_input_tokens", None) or 0,
                cache_read_tokens=getattr(usage, "cache_read_input_tokens", None) or 0,
            ),
            provider_stop_reason=response.stop_reason,
            # A turn that thought goes back whole in a tool round; one that
            # did not is rebuilt from its text and tool calls, as before.
            provider_content=content if thought else None,
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
        # A turn that thought goes back exactly as it came: its thinking
        # blocks, signatures and all, ahead of its tool calls.
        assistant_content: list[dict[str, Any]] = []
        if response.provider_content is not None:
            assistant_content = list(response.provider_content)
        else:
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


# OpenAI's reasoning models reject ``temperature`` with an HTTP 400 unless
# they are told to reason at ``none``. These prefixes name the families that
# do not reason and so always take it; every other model gets it only at
# ``reasoning_effort: none``, so a newly released one works unchanged.
_OPENAI_TEMPERATURE_MODEL_PREFIXES: tuple[str, ...] = ("gpt-3.5", "gpt-4", "chatgpt-4o")

# Models already warned about, so a dropped temperature is logged once per model.
_warned_no_openai_temperature: set[str] = set()


class OpenAIProvider:
    """Wraps openai.AsyncOpenAI, translates to LLMResponse.

    Also supports any OpenAI-compatible API (Ollama, vLLM, Together, Groq,
    LM Studio) via base_url override.

    One request setting comes from an alias's ``provider_config``:
    ``reasoning_effort`` (``none`` to ``max``; not every model takes every
    value). GPT-6 Sol and Luna answer tool calls on Chat Completions only at
    ``none``, and only a model reasoning at ``none`` takes ``temperature``.
    """

    name = "openai"
    # LLMClient hands the calling alias's ``provider_config`` only to a
    # provider that says True here.
    accepts_provider_config = True

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        provider_config: dict[str, Any] | None = None,
    ):
        import openai

        kwargs: dict[str, Any] = {}
        if api_key:
            kwargs["api_key"] = api_key
        if base_url:
            kwargs["base_url"] = base_url
        self._client = openai.AsyncOpenAI(**kwargs)
        # A server that only speaks the OpenAI wire format, not OpenAI itself.
        # The SDK reads OPENAI_BASE_URL when it is handed no base_url.
        self._compatible_server = bool(base_url or os.environ.get("OPENAI_BASE_URL"))
        # The settings for a call that names no alias (see create_message).
        self._provider_config = dict(provider_config or {})

    async def create_message(
        self,
        *,
        model: str,
        messages: list,
        system: str,
        tools: list,
        max_tokens: int,
        temperature: float,
        provider_config: dict[str, Any] | None = None,
    ) -> LLMResponse:
        """Send one request. ``provider_config`` is the calling alias's,
        which ``LLMClient`` hands over per call; its settings replace the
        ones this provider was built with, whole."""
        settings = self._provider_config if provider_config is None else provider_config
        effort = settings.get("reasoning_effort")
        oai_messages: list[dict[str, Any]] = []
        if system:
            oai_messages.append({"role": "system", "content": system})
        oai_messages.extend(messages)

        kwargs: dict[str, Any] = {"model": model, "messages": oai_messages}
        if self._compatible_server:
            # The field every OpenAI-compatible server reads.
            kwargs["max_tokens"] = max_tokens
            kwargs["temperature"] = temperature
        else:
            # Counts reasoning tokens too; reasoning models reject max_tokens.
            kwargs["max_completion_tokens"] = max_tokens
            if effort == "none" or takes_temperature(
                model, _OPENAI_TEMPERATURE_MODEL_PREFIXES,
                asked=temperature, warned=_warned_no_openai_temperature, log=logger,
                reason="only the GPT-4 families and a model reasoning at 'none' take one",
            ):
                kwargs["temperature"] = temperature
        if effort:
            kwargs["reasoning_effort"] = effort
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
            # prompt_tokens includes what OpenAI read from its cache and what
            # it wrote there (GPT-5.6 on); both are priced apart, so they are
            # counted apart, as the Anthropic adapter counts them.
            # A compatible server may send null counts; they read as zero.
            details = getattr(response.usage, "prompt_tokens_details", None)
            cache_read = getattr(details, "cached_tokens", None) or 0
            cache_write = getattr(details, "cache_write_tokens", None) or 0
            prompt_tokens = response.usage.prompt_tokens or 0
            usage = Usage(
                input_tokens=max(prompt_tokens - cache_read - cache_write, 0),
                output_tokens=response.usage.completion_tokens or 0,
                cache_write_tokens=cache_write,
                cache_read_tokens=cache_read,
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
