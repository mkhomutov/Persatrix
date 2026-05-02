"""
Multi-provider LLM client with normalized response types.

Supports Anthropic and OpenAI (including OpenAI-compatible APIs like
Ollama, vLLM, Together, Groq, LM Studio) via a common LLMProvider protocol.
Provider-specific message formats are encapsulated behind the protocol boundary.
"""

import logging
import os
import time
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from .llm_providers import AnthropicProvider, OpenAIProvider
from .llm_types import (
    LLMProvider,
    LLMResponse,
    LLMToolResult,
    StopReason,
    ToolCall,
    Usage,
)
from .observability.metrics import (
    current_agent_id,
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
from .optimization import provider_inference

logger = logging.getLogger(__name__)
_tracer = trace.get_tracer(__name__)


# ─── Re-exported types ──────────────────────────────────────
#
# The normalised dataclasses + ``StopReason`` enum + ``LLMProvider`` Protocol
# live in :mod:`agents.llm_types` (a leaf module with no project-internal
# imports) so this module and :mod:`agents.llm_providers` can both import
# them without inducing a circular import.  Re-exported here to preserve
# the historical ``from agents.llm_client import LLMResponse`` /
# ``StopReason`` / etc. import paths used across the test suite.

__all__ = [
    "AnthropicProvider",
    "LLMClient",
    "LLMProvider",
    "LLMResponse",
    "LLMToolResult",
    "OpenAIProvider",
    "StopReason",
    "ToolCall",
    "Usage",
    "create_provider",
]


# ─── LLM error classification (PR-170 S1) ─────────────────


def _classify_llm_error(exc: BaseException) -> str:
    """Classify an LLM exception into a low-cardinality ``error.type`` bucket.

    Provider SDKs (anthropic, openai) raise their own error types; the
    agent runtime deliberately avoids importing them to stay provider-
    agnostic.  Keyword-match on the exception class name + message instead:
    the resulting buckets are coarse but stable across provider-SDK
    updates and remain within the metric-attribute cardinality budget.
    """
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    if "ratelimit" in name or "rate_limit" in name or "rate limit" in msg or "429" in msg:
        return "rate_limit"
    if "timeout" in name or "timeout" in msg or isinstance(exc, TimeoutError):
        return "timeout"
    return "provider_error"


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
            agent_id = current_agent_id()
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
                    # PR-170 S1: record failure-path duration with
                    # ``llm.success=False`` + a coarse ``error.type`` bucket
                    # so success/failure latency distributions are
                    # separable on dashboards.  Classification is
                    # best-effort by exception class name — provider SDKs
                    # raise their own error types (anthropic, openai) that
                    # the agent runtime does not import, so we keyword-
                    # match without a hard dependency.
                    inst.llm_duration.record(
                        (time.monotonic() - call_started) * 1000.0,
                        attributes=llm_duration_attrs(
                            agent_id=agent_id,
                            request_model=model,
                            success=False,
                            error_type=_classify_llm_error(exc),
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
                        agent_id=agent_id,
                        request_model=model,
                        success=True,
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
# These defaults are overridden by config/optimization.yaml provider_inference.
_OPENAI_EXACT_MODELS: frozenset[str] = frozenset({"o1", "o3", "o4"})
_OPENAI_PREFIX_MODELS: tuple[str, ...] = ("gpt-", "o1-", "o3-", "o4-")


def _infer_provider(model: str) -> str:
    rules = provider_inference()
    anthropic_prefixes = tuple(rules.get("anthropic_prefixes", ("claude",)))
    openai_exact = frozenset(rules.get("openai_exact", _OPENAI_EXACT_MODELS))
    openai_prefixes = tuple(rules.get("openai_prefixes", _OPENAI_PREFIX_MODELS))
    if model.startswith(anthropic_prefixes):
        return "anthropic"
    if model in openai_exact or model.startswith(openai_prefixes):
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
