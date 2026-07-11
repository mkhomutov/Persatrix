"""Google Gemini LLM provider on the native ``google-genai`` SDK.

A first-class :class:`~agents.llm_types.LLMProvider` alongside
``AnthropicProvider`` / ``OpenAIProvider`` (cloud), ``OllamaProvider`` (local),
and ``MockProvider`` (offline). This is the **second concrete dogfood** of the
`RFC 0033 §H <docs/rfcs/0033-model-alias-layer.md>`_ multi-provider
extensibility seam (new class implementing the Protocol + one
``create_provider`` branch + priced alias entries), after Ollama.

**Why a native class, not the OpenAI-compat endpoint.** Gemini exposes an
OpenAI-compatible surface that ``OpenAIProvider`` + ``base_url`` would cover
with zero new code, but that files Gemini traffic under ``openai`` for
cost/telemetry and forfeits native function-calling. RFC 0053 OQ #1 resolved
**native**: a clean ``provider: gemini`` identity (the OTel ``gen_ai.system``
attribute, the derived cost table, and the RFC 0023 lease all key on
``gemini``) and native ``function_declarations`` tool-calling. Unlike the thin
``OllamaProvider`` subclass, Gemini owns its own request build (``contents`` /
``config``), tool mapping, and response normalisation because the wire format
is genuinely different from the Chat Completions shape.

**SDK boundary.** The only calls into ``google-genai`` are ``genai.Client(…)``
(built lazily — see below) and ``client.aio.models.generate_content(…)``. The
tool-definition, message, and response translation is plain-dict based (the
SDK coerces dicts to its pydantic types), so it needs no SDK import and is unit
tested without the optional extra installed.

**Lazy client / missing-key posture (S-09).** ``__init__`` imports the SDK
eagerly so a missing ``google-genai`` install is a startup ``SystemExit`` at
the factory (which catches ``ImportError``), matching every other provider. The
*client* is built lazily on first use so a missing key **warns** at startup and
fails on the first request — the native ``genai.Client`` raises at construction
when no key/env is present, so eager construction would crash a keyless startup
instead of degrading like ``AnthropicProvider`` does.

**provider_config.** Optional. ``project`` + ``location`` route through Vertex
AI (``genai.Client(vertexai=True, …)``); otherwise the default Gemini Developer
API path uses the API key. These mirror OpenAI's ``base_url`` — provider
*configuration* the factory threads from the alias/agent entry.
"""

from __future__ import annotations

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

__all__ = ["GeminiProvider"]


# Gemini reports STOP even when it emits a function call, so TOOL_USE is
# derived from the presence of a function-call part, not the finish reason
# (see ``_normalize``). Only the terminal reasons are mapped here.
_GEMINI_STOP_MAP: dict[str, StopReason] = {
    "STOP": StopReason.END_TURN,
    "MAX_TOKENS": StopReason.MAX_TOKENS,
}

# The protocol's caller emits "user"/"assistant"; Gemini uses "user"/"model".
_ROLE_MAP: dict[str, str] = {
    "user": "user",
    "assistant": "model",
    "model": "model",
}


class GeminiProvider:
    """Wraps ``google.genai.Client``, translates to/from :class:`LLMResponse`."""

    name = "gemini"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        provider_config: dict[str, Any] | None = None,
    ) -> None:
        # Eager import so a missing SDK is a startup SystemExit at the factory
        # (which catches ImportError); the *client* is built lazily below.
        # ``google-genai`` is an optional extra (RFC 0053 OQ #4), unresolved in
        # the type-check env, so the ``google`` namespace (from protobuf) has no
        # ``genai`` attribute to mypy — suppress that one attr-defined probe.
        from google import genai  # type: ignore[attr-defined]

        self._genai = genai
        self._api_key = api_key
        self._provider_config = provider_config or {}
        # The SDK client handle is an untyped optional-import object; typed Any
        # (which subsumes the initial ``None``) so ``_get_client`` and call
        # sites do not each need a union-narrow on an opaque SDK type.
        self._client: Any = None

    def _get_client(self) -> Any:
        """Build the SDK client on first use (lazy — see the module docstring)."""
        if self._client is None:
            project = self._provider_config.get("project")
            location = self._provider_config.get("location")
            if project and location:
                # Vertex AI path.
                self._client = self._genai.Client(
                    vertexai=True, project=project, location=location
                )
            else:
                # Default Gemini Developer API path.
                self._client = self._genai.Client(api_key=self._api_key)
        return self._client

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
        config: dict[str, Any] = {
            "temperature": temperature,
            "max_output_tokens": max_tokens,
        }
        if system:
            config["system_instruction"] = system
        if tools:
            config["tools"] = tools

        response = await self._get_client().aio.models.generate_content(
            model=model,
            contents=self._to_contents(messages),
            config=config,
        )
        return self._normalize(response)

    def _to_contents(self, messages: list) -> list:
        """Map the protocol's messages to Gemini ``contents``.

        The initial turn list is ``[{"role", "content": <str>}]``; a
        multi-round tool loop re-feeds the output of :meth:`append_tool_round`,
        which is already ``[{"role", "parts": [...]}]`` — so a message carrying
        ``parts`` passes through (role-normalised), a ``content`` string becomes
        a single text part, and anything else (a native ``Content``) is passed
        through untouched.
        """
        contents: list = []
        for m in messages:
            if isinstance(m, dict) and "parts" in m:
                role = _ROLE_MAP.get(m.get("role", "user"), "user")
                contents.append({"role": role, "parts": m["parts"]})
            elif isinstance(m, dict) and "content" in m:
                role = _ROLE_MAP.get(m.get("role", "user"), "user")
                contents.append({"role": role, "parts": [{"text": m["content"]}]})
            else:
                contents.append(m)
        return contents

    def _normalize(self, response: Any) -> LLMResponse:
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []

        candidate = None
        candidates = getattr(response, "candidates", None)
        if candidates:
            candidate = candidates[0]

        content = getattr(candidate, "content", None) if candidate else None
        for part in (getattr(content, "parts", None) or []):
            fc = getattr(part, "function_call", None)
            if fc is not None:
                tool_calls.append(
                    ToolCall(
                        # Gemini function calls may carry no id; downstream keys
                        # on id, and Gemini matches function_response by NAME, so
                        # fall back to the name (see append_tool_round).
                        id=getattr(fc, "id", None) or fc.name,
                        name=fc.name,
                        input=dict(getattr(fc, "args", None) or {}),
                    )
                )
                continue
            text = getattr(part, "text", None)
            if text:
                text_parts.append(text)

        # A function-call part means TOOL_USE regardless of the finish reason
        # (Gemini reports STOP even when it emits a function call).
        if tool_calls:
            stop_reason = StopReason.TOOL_USE
        else:
            stop_reason = self._map_finish_reason(candidate)

        return LLMResponse(
            text="\n".join(text_parts) if text_parts else None,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            usage=self._map_usage(response),
        )

    @staticmethod
    def _map_finish_reason(candidate: Any) -> StopReason:
        raw = getattr(candidate, "finish_reason", None) if candidate else None
        if raw is None:
            return StopReason.END_TURN
        # The SDK returns a FinishReason enum (``.name``); tolerate a bare str.
        name = getattr(raw, "name", None) or (raw if isinstance(raw, str) else None)
        mapped = _GEMINI_STOP_MAP.get(name) if name else None
        if mapped is None:
            logger.warning(
                "Unmapped Gemini finish_reason %r, defaulting to END_TURN", name
            )
            return StopReason.END_TURN
        return mapped

    @staticmethod
    def _map_usage(response: Any) -> Usage:
        meta = getattr(response, "usage_metadata", None)
        if meta is None:
            return Usage(0, 0)
        return Usage(
            input_tokens=getattr(meta, "prompt_token_count", 0) or 0,
            output_tokens=getattr(meta, "candidates_token_count", 0) or 0,
        )

    def format_tool_definitions(self, tools: list[dict]) -> list[dict]:
        """Map the protocol's tool definitions to a single Gemini ``Tool``.

        Gemini groups function declarations under one ``Tool``, so N protocol
        tools become one ``{"function_declarations": [...]}`` entry. The
        ``parameters`` JSON schema is passed through verbatim, exactly as the
        Anthropic/OpenAI providers pass theirs.
        """
        if not tools:
            return []
        return [
            {
                "function_declarations": [
                    {
                        "name": t["name"],
                        "description": t["description"],
                        "parameters": t["parameters"],
                    }
                    for t in tools
                ]
            }
        ]

    def append_tool_round(
        self,
        messages: list,
        response: LLMResponse,
        tool_results: list[LLMToolResult],
    ) -> list:
        """Append the model's tool-call turn + the tool results as Gemini turns.

        The model turn carries any text plus one ``function_call`` part per
        call; the user turn carries one ``function_response`` part per result,
        keyed by the tool **name** (Gemini correlates results by name, so the
        result's ``tool_call_id`` is mapped back to its name via the response's
        tool calls).
        """
        name_by_id = {tc.id: tc.name for tc in response.tool_calls}

        model_parts: list[dict[str, Any]] = []
        if response.text:
            model_parts.append({"text": response.text})
        for tc in response.tool_calls:
            fc: dict[str, Any] = {"name": tc.name, "args": tc.input}
            if tc.id:
                fc["id"] = tc.id
            model_parts.append({"function_call": fc})

        user_parts: list[dict[str, Any]] = []
        for tr in tool_results:
            name = name_by_id.get(tr.tool_call_id, tr.tool_call_id)
            payload = {"error": tr.content} if tr.is_error else {"output": tr.content}
            user_parts.append(
                {"function_response": {"name": name, "response": payload}}
            )

        return [
            *messages,
            {"role": "model", "parts": model_parts},
            {"role": "user", "parts": user_parts},
        ]
