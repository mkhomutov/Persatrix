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

from .call_log import record_call
from .generated import wallet_pb2 as walletpb
from .llm_call_support import (
    _classify_llm_error,
    _current_trace_id,
    _estimate_input_tokens,
)
from .llm_factory import LaneProviderError, create_provider, provider_for_resolved
from .llm_gemini import GeminiProvider
from .llm_offline import MockProvider
from .llm_ollama import OllamaProvider
from .llm_providers import AnthropicProvider, OpenAIProvider
from .llm_types import (
    CallPurpose,
    LLMProvider,
    LLMResponse,
    LLMToolResult,
    StopReason,
    ToolCall,
    Usage,
)
from .llm_watsonx import WatsonxProvider
from .model_aliases import resolve as resolve_model
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
    "CallPurpose",
    "GeminiProvider",
    "LLMClient",
    "LaneProviderError",
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
    "WatsonxProvider",
    "create_provider",
]


# ─── LLM Client Facade ──────────────────────────────────────


# The provider classes ``create_provider`` itself constructs (OllamaProvider is
# an OpenAIProvider subclass — covered). ISSUE-0113 lane routing is defined
# ONLY between these: a primary provider outside this set (a test double, or
# the RFC 0044 eval Replay/Recording wrappers) keeps the legacy single-provider
# contract, so wrappers observe every call — including the lanes'.
_FACTORY_PROVIDER_CLASSES: tuple[type, ...] = (
    AnthropicProvider,
    GeminiProvider,
    MockProvider,
    OpenAIProvider,
    WatsonxProvider,
)


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
        purpose: CallPurpose | None = None,
        cache_prefix: str = "",
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
        ``model``) and is **never** forwarded to the provider, so the vendor
        API receives the physical id only. Since ISSUE-0113 it is also the
        cross-provider routing key: an alias whose record declares a
        *different* vendor than this client's own provider routes the call
        through a provider built from that record (see
        :meth:`_provider_for_alias`), so shared role lanes work on
        mixed-vendor rosters.

        *purpose* says what the call is for; it goes to the call log (see
        :mod:`agents.call_log`) and never to the provider.

        *cache_prefix* is stable system text the provider should cache, such
        as EXP-001 arm D′'s earlier transcripts. A provider that can cache
        (``supports_prompt_cache is True``) sends it marked for the cache,
        ahead of *system*; any other gets it joined onto the front of
        *system*, so the model reads the same words either way.
        """
        provider = self._provider_for_alias(model_alias)
        if cache_prefix:
            if getattr(provider, "supports_prompt_cache", False) is True:
                kwargs["cache_prefix"] = cache_prefix
            else:
                system = kwargs.get("system") or ""
                kwargs["system"] = f"{cache_prefix}\n\n{system}" if system else cache_prefix
        if self._wallet is None or cause == walletpb.CAUSE_UNSPECIFIED:
            return await self._invoke_provider(
                kwargs, provider=provider, model_alias=model_alias, purpose=purpose,
            )
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
                kwargs, provider=provider, lease=lease, model_alias=model_alias,
                purpose=purpose,
            )
            await lease.settle(
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            )
            return response

    def _provider_for_alias(self, model_alias: str | None) -> LLMProvider:
        """The provider a call arriving via *model_alias* must ride (ISSUE-0113).

        A persona holds ONE client, built from its own seat alias — but the
        shared role lanes (``fast`` bid / ``summarizer`` close / critic /
        memory compression) resolve *their* alias per-call and pass it here.
        When that alias's record declares a **different vendor** than this
        client's own provider, the call routes through a provider built from
        the resolved record (cached process-wide in
        :func:`agents.llm_factory.provider_for_resolved`) — RFC 0033 §D as
        stated: the alias entry is the joint declaration of provider + model.
        Pre-fix, a non-matching seat sent the lane's model ID to its own
        vendor and 404'd, muting governed bidding on mixed-vendor rosters.

        Everything else keeps the primary provider byte-for-byte:

        * no ``model_alias`` (raw/test paths);
        * a primary provider that is not a **factory-built vendor client**
          (:data:`_FACTORY_PROVIDER_CLASSES`) — test doubles, and the RFC
          0044 eval replay/recording wrappers, whose contract is that
          EVERY call stays visible to the wrapper (routing a lane call
          around the cassette breaks replay — caught by
          ``test_seed_recipe_replays_green`` in CI);
        * an alias that fails to resolve — every lane already bails on its
          own resolve failure *before* calling, so this arm is defensive;
        * an alias resolving to the **same** vendor name (the persona's own
          turns, and every single-vendor overlay — demos/MTs unchanged).

        The vendor-name comparison deliberately keys on the resolved
        ``provider`` field only: two ``openai``-named endpoints with
        different ``base_url``\\ s still share the primary client, the
        pre-fix behaviour — ISSUE-0113 is scoped to cross-vendor lanes.
        Tool-definition formatting / tool rounds stay on the primary client;
        lanes pass ``tools=[]`` and never run tool rounds.

        A lane provider that cannot be *built* (missing SDK / key posture)
        raises :class:`LaneProviderError` — a plain ``Exception`` — so lane
        callers degrade fail-closed instead of falling back to the primary
        provider's guaranteed 404.
        """
        if not model_alias:
            return self._provider
        if not isinstance(self._provider, _FACTORY_PROVIDER_CLASSES):
            return self._provider
        try:
            resolved = resolve_model(model_alias)
        except SystemExit:
            return self._provider
        if resolved.provider == self._provider.name:
            return self._provider
        return provider_for_resolved(resolved)

    async def _invoke_provider(
        self,
        kwargs: dict[str, Any],
        *,
        provider: LLMProvider | None = None,
        lease: Lease | None = None,
        model_alias: str | None = None,
        purpose: CallPurpose | None = None,
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

        *provider* (ISSUE-0113) is the effective provider for THIS call —
        the primary one, or a cross-vendor lane client picked by
        :meth:`_provider_for_alias` — so the span's ``gen_ai.system``
        reports the vendor actually contacted. ``None`` means the primary.

        Every call, finished or failed, ends with one call-log line
        (:func:`agents.call_log.record_call`), written only when the log is on.
        """
        if provider is None:
            provider = self._provider
        model = str(kwargs.get("model", ""))
        # Prefer the provider's declared ``name`` attribute (Protocol
        # contract).  Fall back to a lower-cased class-name derivation only
        # when the attribute is missing or not a string — this keeps test
        # doubles (``AsyncMock``) working without surfacing them as the
        # ``gen_ai.system`` value in production traces.
        provider_name = getattr(provider, "name", None)
        if isinstance(provider_name, str) and provider_name:
            system_name = provider_name
        else:
            system_name = (
                type(provider).__name__.replace("Provider", "").lower()
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
            call_started_wall = time.time()

            def _log(usage: Usage | None, error: BaseException | None) -> None:
                record_call(
                    started_at=call_started_wall, purpose=purpose, provider=system_name,
                    model=model, model_alias=model_alias, usage=usage, error=error,
                )

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
                response = await provider.create_message(**kwargs)
            except Exception as exc:
                _log(None, exc)
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
            except BaseException as exc:
                # A cancelled call (asyncio.wait_for) may still have been billed.
                _log(None, exc)
                raise
            _log(response.usage, None)
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
