"""
Multi-provider LLM client with normalized response types.

Supports Anthropic and OpenAI (including OpenAI-compatible APIs like
vLLM, Together, Groq, LM Studio) via a common LLMProvider protocol, plus a
first-class local-model provider for Ollama (a thin OpenAI-compatible
subclass, see :mod:`agents.llm_ollama`) and a zero-cost offline mock
(:mod:`agents.llm_offline`). Provider-specific message formats are
encapsulated behind the protocol boundary.
"""

import logging
import time
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from .generated import wallet_pb2 as walletpb
from .llm_factory import create_provider
from .llm_gemini import GeminiProvider
from .llm_offline import MockProvider
from .llm_ollama import OllamaProvider
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
    LLM_MODEL_ALIAS_ATTR,
    STOP_REASON_TO_GEN_AI,
    gen_ai_attributes,
)
from .wallet_client import BudgetExceededError, Lease, WalletClient

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
    "BudgetExceededError",
    "GeminiProvider",
    "LLMClient",
    "LLMProvider",
    "LLMResponse",
    "LLMToolResult",
    "MockProvider",
    "OllamaProvider",
    "OpenAIProvider",
    "StopReason",
    "ToolCall",
    "Usage",
    "WalletClient",
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


# ─── Wallet-lease helpers (RFC 0023 PR 3) ───────────────────


def _current_trace_id() -> str:
    """Return the active OTEL trace ID as a 32-hex string, or ``""``.

    Threaded onto the ``LeaseRequest`` as ``trace_id`` so the wallet-side
    lease logs correlate with the agent's LLM-call span (RFC 0023 § C)."""
    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return ""
    return trace.format_trace_id(ctx.trace_id)


def _estimate_input_tokens(kwargs: dict[str, Any]) -> int:
    """Estimate the prompt's input-token count for the lease request.

    Reuses the project-wide ``cl100k_base`` tokeniser (RFC 0023 Open
    Question §5 — a single tokeniser path system-wide). The estimate
    funds the lease's *provisional* charge only; ``SettleLease``
    reconciles it to the provider-reported actuals, so a best-effort
    flatten of the system prompt + message text is sufficient. Tool
    definitions (``kwargs["tools"]``) are deliberately *not* counted:
    serialising every tool schema would add complexity for no
    enforcement benefit — settle reconciles to actuals, so an estimate
    that under-counts only shrinks the provisional hold, never the
    final charge. A tokeniser import failure degrades to the chars/4
    fallback rather than blocking the call."""
    parts: list[str] = []
    system = kwargs.get("system")
    if isinstance(system, str) and system:
        parts.append(system)
    for msg in kwargs.get("messages") or []:
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                text = block.get("text")
                parts.append(text if isinstance(text, str) else str(block.get("content") or ""))
    text = "\n".join(p for p in parts if p)
    try:
        from .persona_runtime.memory_budget import _count_tokens

        return _count_tokens(text)
    except Exception:  # pragma: no cover — estimation must never block a call
        logger.debug("token estimate fell back to chars/4", exc_info=True)
        return max(0, len(text) // 4)


# ─── LLM Client Facade ──────────────────────────────────────


class LLMClient:
    """Provider-agnostic LLM client. Delegates to a concrete LLMProvider."""

    def __init__(self, provider: LLMProvider, wallet: WalletClient | None = None):
        self._provider = provider
        # RFC 0023 — the wallet is optional and wired post-construction by
        # AgentServer.start() (see set_wallet); LLMClient is built at agent
        # load time, before the orchestrator gRPC channel exists.
        self._wallet = wallet

    def set_wallet(self, wallet: WalletClient | None) -> None:
        """Attach (or replace) the RFC 0023 wallet client.

        Called by ``AgentServer.start()`` once the shared orchestrator
        gRPC channel is open — the LLMClient is constructed earlier, at
        agent load time, when no channel exists yet."""
        self._wallet = wallet

    async def create_message(
        self,
        *,
        cause: walletpb.Cause.ValueType = walletpb.CAUSE_UNSPECIFIED,
        workflow_id: str = "",
        agent_id: str = "",
        interaction_id: str = "",
        model_alias: str | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Invoke the provider, optionally bracketed by an RFC 0023 wallet lease.

        When a wallet is attached *and* *cause* is not ``CAUSE_UNSPECIFIED``,
        the provider call is wrapped in ``WalletClient.lease(...)``: a
        server-issued lease is acquired before the call and settled with the
        provider-reported actual usage after it. :class:`BudgetExceededError`
        propagates to the caller when the wallet denies the lease or is
        unreachable — the call fails *closed* (RFC 0023 § F).

        Without a wallet, or with no *cause*, the provider is invoked
        directly: that is the un-migrated v0.2.3 path PRs 4–6 wire for the
        chat / autonomous-TICK / sub-agent / channel-message origins.

        *interaction_id* (RFC 0030 producer plan PR 2) names the
        orchestrator-resolved interaction the triggering event belongs to —
        the Layer 1 attribution substrate. The wallet only tracks (and can
        only deny) an interaction when a positive
        ``interaction_budget_tokens`` accompanies the id on the same lease
        request (``internal/wallet/wallet.go``); until the config-stamping
        follow-up threads that ceiling, the id rides the wire and the
        wallet discards it. Empty (the default) is the untracked case —
        every ceiling stays at its uncapped default, the pre-producer
        behaviour.

        *model_alias* (RFC 0033 §G) is the logical alias the caller resolved
        ``model`` from, when it came in via one. It is emitted as the
        ``persatrix.llm.model_alias`` span attribute (alongside the physical
        ``model``) and is a telemetry-only concern — it is **never** forwarded
        to the provider, so the vendor API receives the physical id only.
        """
        if self._wallet is None or cause == walletpb.CAUSE_UNSPECIFIED:
            return await self._invoke_provider(kwargs, model_alias=model_alias)
        async with self._wallet.lease(
            agent_id=agent_id,
            model=str(kwargs.get("model", "")),
            estimated_input_tokens=_estimate_input_tokens(kwargs),
            estimated_max_output_tokens=int(kwargs.get("max_tokens", 0) or 0),
            cause=cause,
            workflow_id=workflow_id,
            trace_id=_current_trace_id(),
            interaction_id=interaction_id,
        ) as lease:
            response = await self._invoke_provider(
                kwargs, lease=lease, model_alias=model_alias,
            )
            await lease.settle(
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            )
            return response

    async def _invoke_provider(
        self,
        kwargs: dict[str, Any],
        *,
        lease: Lease | None = None,
        model_alias: str | None = None,
    ) -> LLMResponse:
        """Invoke the underlying provider, wrapped in an ``agent.llm.call`` span.

        Span attributes follow the OTEL Gen-AI semantic conventions
        (``gen_ai.system``, ``gen_ai.request.model``,
        ``gen_ai.usage.input_tokens`` / ``output_tokens``,
        ``gen_ai.response.finish_reasons``) so vendor backends render
        Persatrix LLM traces without project-specific configuration
        (RFC 0019 § D / § E). When *lease* is set, the lease ID is emitted
        as the ``persatrix.lease_id`` span attribute for correlation with
        the wallet-side lease logs (RFC 0023 § E).

        When *model_alias* is set (the request came in via a models.aliases
        name), it is emitted as the ``persatrix.llm.model_alias`` attribute
        (RFC 0033 §G) — added alongside the physical ``gen_ai.request.model``,
        not substituted for it, and omitted entirely on the raw-ID path.
        ``model_alias`` is not part of *kwargs*, so it never reaches the
        provider call below.
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
            if lease is not None:
                span.set_attribute("persatrix.lease_id", lease.lease_id)
            if model_alias:
                span.set_attribute(LLM_MODEL_ALIAS_ATTR, model_alias)
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
                if lease is not None:
                    # The provider is about to be contacted — an exception
                    # from here on closes the lease at the granted amount
                    # (settle-at-granted), not via release (RFC 0023 § F).
                    lease.mark_call_started()
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


# RFC 0033 §I / Phase 3: provider selection flows exclusively through the
# alias map's declared ``provider`` field (:func:`agents.model_aliases.resolve`)
# — provider is data, not inferred. The raw-ID prefix-routing heuristic
# ``_infer_provider`` (and its ``_OPENAI_*`` prefix tables) was retired with the
# §E raw-vendor-ID pass-through; a reference that is not a declared alias is now
# a loud ``SystemExit`` at resolve, not a silent prefix-inferred route.


# ``create_provider`` lives in :mod:`agents.llm_factory` and is re-exported
# here (and in ``__all__``) so the historical
# ``from agents.llm_client import create_provider`` import path is preserved.
