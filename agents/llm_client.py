"""
Multi-provider LLM client with normalized response types.

Supports Anthropic and OpenAI (including OpenAI-compatible APIs like
Ollama, vLLM, Together, Groq, LM Studio) via a common LLMProvider protocol.
Provider-specific message formats are encapsulated behind the protocol boundary.
"""

import json
import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from .observability.spans import LLM_CALL_SPAN, gen_ai_attributes

logger = logging.getLogger(__name__)
_tracer = trace.get_tracer(__name__)


# ─── Normalized Types ───────────────────────────────────────


class StopReason(Enum):
    """Provider-agnostic stop reason.

    Unmapped provider-specific stop reasons (e.g. Anthropic's stop_sequence,
    OpenAI's content_filter) are mapped to END_TURN with a warning log.
    """

    END_TURN = "end_turn"
    TOOL_USE = "tool_use"
    MAX_TOKENS = "max_tokens"


@dataclass
class ToolCall:
    """Provider-agnostic tool call."""

    id: str
    name: str
    input: dict[str, Any]


@dataclass
class Usage:
    """Token usage from LLM response."""

    input_tokens: int
    output_tokens: int


@dataclass
class LLMToolResult:
    """Provider-agnostic tool result for LLM message building.

    Not to be confused with tools.registry.ToolResult which represents
    the raw result from a tool function (success/data/error/error_type).
    """

    tool_call_id: str
    content: str
    is_error: bool


@dataclass
class LLMResponse:
    """Normalized response from any LLM provider."""

    text: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: StopReason = StopReason.END_TURN
    usage: Usage = field(default_factory=lambda: Usage(0, 0))


# ─── Provider Protocol ──────────────────────────────────────


class LLMProvider(Protocol):
    """Protocol for LLM provider implementations."""

    async def create_message(
        self,
        *,
        model: str,
        messages: list,
        system: str,
        tools: list,
        max_tokens: int,
        temperature: float,
    ) -> LLMResponse: ...

    def format_tool_definitions(self, tools: list[dict]) -> list[dict]: ...

    def append_tool_round(
        self,
        messages: list,
        response: LLMResponse,
        tool_results: list[LLMToolResult],
    ) -> list: ...


# ─── Anthropic Provider ─────────────────────────────────────


_ANTHROPIC_STOP_MAP: dict[str, StopReason] = {
    "end_turn": StopReason.END_TURN,
    "tool_use": StopReason.TOOL_USE,
    "max_tokens": StopReason.MAX_TOKENS,
}


class AnthropicProvider:
    """Wraps anthropic.AsyncAnthropic, translates to LLMResponse."""

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


# ─── LLM Client Facade ──────────────────────────────────────


class LLMClient:
    """Provider-agnostic LLM client. Delegates to a concrete LLMProvider."""

    def __init__(self, provider: LLMProvider):
        self._provider = provider

    async def create_message(self, **kwargs: Any) -> LLMResponse:
        """Invoke the underlying provider, wrapped in an ``agent.llm.call`` span.

        Span attributes follow the OTEL Gen-AI semantic conventions
        (``gen_ai.system``, ``gen_ai.request.model``,
        ``gen_ai.usage.input_tokens`` / ``output_tokens``,
        ``gen_ai.response.finish_reasons``) so vendor backends render
        Persatrix LLM traces without project-specific configuration
        (RFC 0019 § D / § E).
        """
        model = str(kwargs.get("model", ""))
        system_name = type(self._provider).__name__.replace("Provider", "").lower()
        with _tracer.start_as_current_span(
            LLM_CALL_SPAN,
            attributes=gen_ai_attributes(
                system=system_name,
                request_model=model,
            ),
        ) as span:
            try:
                response = await self._provider.create_message(**kwargs)
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                raise
            for k, v in gen_ai_attributes(
                system=system_name,
                request_model=model,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                finish_reasons=[response.stop_reason.value],
            ).items():
                span.set_attribute(k, v)
            return response

    def format_tool_definitions(self, tools: list[dict]) -> list[dict]:
        return self._provider.format_tool_definitions(tools)

    def append_tool_round(
        self,
        messages: list,
        response: LLMResponse,
        tool_results: list[LLMToolResult],
    ) -> list:
        return self._provider.append_tool_round(messages, response, tool_results)


# ─── Provider Factory ────────────────────────────────────────


# S-14: Separate exact matches from prefix matches for o-series models.
# ``startswith("o1")`` would match "o10", "o100" etc. Instead, use exact
# matches for bare model names and prefix matches for versioned names.
_OPENAI_EXACT_MODELS: frozenset[str] = frozenset({"o1", "o3", "o4"})
_OPENAI_PREFIX_MODELS: tuple[str, ...] = ("gpt-", "o1-", "o3-", "o4-")


def _infer_provider(model: str) -> str:
    if model.startswith("claude"):
        return "anthropic"
    if model in _OPENAI_EXACT_MODELS or model.startswith(_OPENAI_PREFIX_MODELS):
        return "openai"
    logger.warning(
        "Unknown model prefix %r, defaulting to openai provider", model
    )
    return "openai"


def create_provider(agent_config: dict[str, Any]) -> LLMProvider:
    """Create an LLM provider from agent config.

    Supports explicit ``provider`` field or inference from model prefix.
    """
    model = agent_config["model"]
    # S-18: guard against empty model string — "" falls through
    # _infer_provider() to "openai" fallback, causing a confusing error.
    if not model:
        raise SystemExit("Agent config 'model' field is empty")
    provider = agent_config.get("provider") or _infer_provider(model)
    provider_config = agent_config.get("provider_config", {})

    if provider == "anthropic":
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        # S-09: Warn at startup if API key is unset for non-local providers
        # so operators get a clear message instead of a confusing auth error
        # on the first task.
        if not api_key:
            logger.warning(
                "ANTHROPIC_API_KEY not set — Anthropic provider will fail on first request"
            )
        # PR-review SF1: Surface a clear install instruction instead of a
        # raw ImportError traceback when the SDK package is missing.
        try:
            return AnthropicProvider(api_key=api_key)
        except ImportError:
            raise SystemExit(
                "Provider 'anthropic' requires package 'anthropic'. "
                "Install with: pip install 'anthropic>=0.40.0'"
            )
    elif provider == "openai":
        base_url = provider_config.get("base_url")
        api_key = os.environ.get("OPENAI_API_KEY")
        # S-09: Only warn for non-local providers (base_url implies local/custom).
        if not api_key and not base_url:
            logger.warning(
                "OPENAI_API_KEY not set — OpenAI provider will fail on first request"
            )
        try:
            return OpenAIProvider(
                api_key=api_key,
                base_url=base_url,
            )
        except ImportError:
            raise SystemExit(
                "Provider 'openai' requires package 'openai'. "
                "Install with: pip install 'openai>=1.50.0'"
            )
    raise SystemExit(f"Unknown LLM provider: {provider!r}")
