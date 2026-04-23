"""
Multi-provider LLM client with normalized response types.

Supports Anthropic and OpenAI (including OpenAI-compatible APIs like
Ollama, vLLM, Together, Groq, LM Studio) via a common LLMProvider protocol.
Provider-specific message formats are encapsulated behind the protocol boundary.
"""

import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from .observability.metrics import (
    llm_call_attrs,
    llm_duration_attrs,
    llm_token_attrs,
    try_get_instruments,
)
from .observability.spans import (
    LLM_CALL_SPAN,
    STOP_REASON_TO_GEN_AI,
    gen_ai_attributes,
)

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

    # Stable identifier emitted as the OTEL ``gen_ai.system`` attribute
    # (``"anthropic"``, ``"openai"``, …).  Declared here so call sites do
    # not have to derive it from ``type().__name__`` (which silently
    # produces wrong values for test doubles like ``AsyncMock``).
    name: str

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


# ─── Provider Implementations (re-exported) ────────────────


# Provider classes live in :mod:`agents.llm_providers` so this module stays
# under the 500-line review-friendly cap. They are re-exported here to keep
# the historical ``from agents.llm_client import AnthropicProvider`` /
# ``OpenAIProvider`` import paths working.
from .llm_providers import AnthropicProvider, OpenAIProvider  # noqa: E402, I001


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
        # Prefer the provider's declared ``name`` attribute (Protocol
        # contract).  Fall back to a lower-cased class-name derivation only
        # when the attribute is missing or not a string — this keeps test
        # doubles (``AsyncMock``) working without surfacing them as the
        # ``gen_ai.system`` value in production traces.
        provider_name = getattr(self._provider, "name", None)
        if isinstance(provider_name, str) and provider_name:
            system_name = provider_name
        else:
            system_name = (
                type(self._provider).__name__.replace("Provider", "").lower()
            )
        with _tracer.start_as_current_span(
            LLM_CALL_SPAN,
            attributes=gen_ai_attributes(
                system=system_name,
                request_model=model,
            ),
        ) as span:
            call_started = time.monotonic()
            agent_id = os.environ.get("PERSATRIX_AGENT_ID", "unknown")
            inst = try_get_instruments()
            if inst is not None:
                inst.llm_calls.add(
                    1,
                    attributes=llm_call_attrs(
                        agent_id=agent_id,
                        system=system_name,
                        request_model=model,
                    ),
                )
            try:
                response = await self._provider.create_message(**kwargs)
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                if inst is not None:
                    inst.llm_duration.record(
                        (time.monotonic() - call_started) * 1000.0,
                        attributes=llm_duration_attrs(
                            agent_id=agent_id, request_model=model,
                        ),
                    )
                raise
            # Translate Persatrix-internal StopReason values to the OTEL
            # Gen-AI canonical vocabulary so vendor backends render the
            # ``gen_ai.response.finish_reasons`` attribute correctly.
            # Unknown values fall through to ``"error"`` per the spec's
            # generic bucket — this should never fire today (the enum is
            # closed) but future StopReason additions degrade gracefully.
            canonical_reason = STOP_REASON_TO_GEN_AI.get(
                response.stop_reason.value, "error",
            )
            span.set_attributes(
                gen_ai_attributes(
                    system=system_name,
                    request_model=model,
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                    finish_reasons=[canonical_reason],
                ),
            )
            if inst is not None:
                duration_ms = (time.monotonic() - call_started) * 1000.0
                inst.llm_duration.record(
                    duration_ms,
                    attributes=llm_duration_attrs(
                        agent_id=agent_id, request_model=model,
                    ),
                )
                inst.llm_tokens.add(
                    response.usage.input_tokens,
                    attributes=llm_token_attrs(
                        agent_id=agent_id,
                        request_model=model,
                        token_type="input",
                    ),
                )
                inst.llm_tokens.add(
                    response.usage.output_tokens,
                    attributes=llm_token_attrs(
                        agent_id=agent_id,
                        request_model=model,
                        token_type="output",
                    ),
                )
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
