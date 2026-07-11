"""IBM watsonx.ai LLM provider on the native ``ibm-watsonx-ai`` SDK.

A first-class :class:`~agents.llm_types.LLMProvider` alongside
``AnthropicProvider`` / ``OpenAIProvider`` / ``GeminiProvider`` (cloud),
``OllamaProvider`` (local), and ``MockProvider`` (offline). Like Gemini, this is
a dogfood of the `RFC 0033 §H <docs/rfcs/0033-model-alias-layer.md>`_
multi-provider extensibility seam (new class implementing the Protocol + one
``create_provider`` branch + priced alias entries).

**Why a native class, not a base_url reuse.** watsonx exposes no broad
OpenAI-compatible *endpoint* that ``OpenAIProvider`` + ``base_url`` could reach,
so a native class over the ``ibm-watsonx-ai`` SDK's ``ModelInference`` is
required (RFC 0053 §C). But watsonx's *chat wire format* IS OpenAI-shaped — a
``choices[].message`` dict with ``tool_calls`` whose ``function.arguments`` is a
JSON string, plus an OpenAI-style ``usage`` block — so the translation
deliberately mirrors ``OpenAIProvider`` (tool-definition and tool-round mapping
are byte-identical) while only the transport differs.

**Per-model client (lazy + cached).** ``ModelInference`` binds a single
``model_id`` at construction, so — unlike the other providers' one shared client
— a :class:`WatsonxProvider` holds a small cache keyed by physical model id,
built lazily on first use (a society usually names two: ``quality`` and
``fast``/``summarizer``). ``__init__`` imports the SDK eagerly so a missing
``ibm-watsonx-ai`` install is a startup ``SystemExit`` at the factory (which
catches ``ImportError``), matching every other provider; only the *client* is
built lazily.

**Sync chat offloaded to a thread.** ``ModelInference.chat`` is a blocking call;
the SDK's async surface has varied across releases, so this provider offloads
the stable, always-present ``chat`` to :func:`asyncio.to_thread` rather than
depend on a version-specific ``achat`` — the event loop is never blocked, and
the provider works against any SDK version that has ``chat``.

**Auth vs. config.** The secret IBM Cloud IAM key rides ``WATSONX_API_KEY``
(env, threaded in by the factory). The **required** ``url`` (regional endpoint)
and ``project_id`` (or ``space_id``) are **config, not secrets** — the factory
reads them from the alias ``provider_config`` (the same channel OpenAI's
``base_url`` uses) and **fails closed** if either is absent (they are required to
construct the client at all, so the failure is loud at startup, not the softer
missing-key warning). See :func:`agents.llm_factory.create_provider`.

**Tool calls.** watsonx's chat API supports ``tools`` for tool-capable models
(Llama 3.x, Granite, Mistral Large, …). A model without native tool support
degrades to no-tool turns — a per-model catalog constraint, not a blocker for
the RFC 0052 brainstorm demo, which is conversation, not tool use.
"""

from __future__ import annotations

import asyncio
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

__all__ = ["WatsonxProvider"]


# watsonx chat reports OpenAI-style finish reasons; ``eos_token`` is a
# foundation-model natural-completion signal folded into END_TURN. TOOL_USE is
# also derived from the *presence* of a tool_calls part (see ``_normalize``), so
# a model that reports ``stop`` alongside a tool call still routes correctly.
_WATSONX_STOP_MAP: dict[str, StopReason] = {
    "stop": StopReason.END_TURN,
    "eos_token": StopReason.END_TURN,
    "tool_calls": StopReason.TOOL_USE,
    "length": StopReason.MAX_TOKENS,
}


class WatsonxProvider:
    """Wraps ``ibm_watsonx_ai`` ``ModelInference``, translates to/from
    :class:`LLMResponse`."""

    name = "watsonx"

    def __init__(
        self,
        *,
        api_key: str | None,
        url: str,
        project_id: str | None = None,
        space_id: str | None = None,
    ) -> None:
        # Eager import so a missing SDK is a startup SystemExit at the factory
        # (which catches ImportError); the per-model *client* is built lazily
        # below. Both names are optional-extra imports (RFC 0053 OQ #4),
        # unresolved in the type-check env — suppress the missing-import probe.
        from ibm_watsonx_ai import Credentials  # type: ignore[import-not-found]
        from ibm_watsonx_ai.foundation_models import (  # type: ignore[import-not-found]
            ModelInference,
        )

        self._Credentials = Credentials
        self._ModelInference = ModelInference
        self._api_key = api_key
        self._url = url
        self._project_id = project_id
        self._space_id = space_id
        # model_id -> ModelInference (untyped optional-import object, so Any).
        self._models: dict[str, Any] = {}

    def _get_model(self, model_id: str) -> Any:
        """Build (and cache) the ``ModelInference`` bound to ``model_id``.

        ``ModelInference`` is per-model, so this caches one client per physical
        id — a chat turn should not reconstruct the client each call.
        """
        model = self._models.get(model_id)
        if model is None:
            credentials = self._Credentials(url=self._url, api_key=self._api_key)
            kwargs: dict[str, Any] = {"model_id": model_id, "credentials": credentials}
            # project_id wins when both are set; the factory guarantees one is
            # present (fail-closed), so a client is always constructible here.
            if self._project_id:
                kwargs["project_id"] = self._project_id
            elif self._space_id:
                kwargs["space_id"] = self._space_id
            model = self._ModelInference(**kwargs)
            self._models[model_id] = model
        return model

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
        wx_messages: list[dict[str, Any]] = []
        if system:
            wx_messages.append({"role": "system", "content": system})
        wx_messages.extend(messages)

        chat_kwargs: dict[str, Any] = {
            "messages": wx_messages,
            "params": {"max_tokens": max_tokens, "temperature": temperature},
        }
        if tools:
            chat_kwargs["tools"] = tools

        model_inference = self._get_model(model)
        # ``chat`` is synchronous/blocking; offload it so the event loop keeps
        # servicing other agents (see the module docstring on the async choice).
        response = await asyncio.to_thread(lambda: model_inference.chat(**chat_kwargs))
        return self._normalize(response)

    def _normalize(self, response: dict) -> LLMResponse:
        choices = response.get("choices") or []
        if not choices:
            # No choices at all — surface it rather than degrade to a silent
            # empty END_TURN with no operator signal (the GeminiProvider
            # no-candidates precedent).
            logger.warning(
                "watsonx response carried no choices; defaulting to empty END_TURN"
            )
            return LLMResponse(
                text=None,
                tool_calls=[],
                stop_reason=StopReason.END_TURN,
                usage=self._map_usage(response),
            )

        choice = choices[0]
        message = choice.get("message") or {}

        tool_calls: list[ToolCall] = []
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function") or {}
            name = fn.get("name", "")
            tool_calls.append(
                ToolCall(
                    # watsonx tool calls carry an id, but fall back to the name
                    # (downstream keys on id) to match the other providers.
                    id=tc.get("id") or name,
                    name=name,
                    input=self._parse_arguments(fn.get("arguments"), name),
                )
            )

        # A tool_calls part means TOOL_USE regardless of the finish reason.
        if tool_calls:
            stop_reason = StopReason.TOOL_USE
        else:
            stop_reason = self._map_finish_reason(choice.get("finish_reason"))

        return LLMResponse(
            # Normalise an empty/absent content string to None (no-text
            # semantics), matching the Anthropic/Gemini providers.
            text=message.get("content") or None,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            usage=self._map_usage(response),
        )

    @staticmethod
    def _parse_arguments(raw: Any, tool_name: str) -> dict[str, Any]:
        """Decode a tool call's ``function.arguments`` to a dict.

        watsonx (like OpenAI) returns arguments as a JSON *string*; some
        models/SDK versions hand back an already-parsed dict. Invalid JSON
        falls back to an empty dict so the agent loop keeps running (the
        ``OpenAIProvider`` M2 fix).
        """
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str) and raw:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning(
                    "Invalid JSON in watsonx tool call arguments for %s, "
                    "falling back to empty input",
                    tool_name,
                )
                return {}
            # Guard against non-object JSON (an array / scalar) reaching the tool
            # runner as its input dict.
            return parsed if isinstance(parsed, dict) else {}
        return {}

    @staticmethod
    def _map_finish_reason(raw: Any) -> StopReason:
        if raw is None:
            return StopReason.END_TURN
        mapped = _WATSONX_STOP_MAP.get(raw) if isinstance(raw, str) else None
        if mapped is None:
            logger.warning(
                "Unmapped watsonx finish_reason %r, defaulting to END_TURN", raw
            )
            return StopReason.END_TURN
        return mapped

    @staticmethod
    def _map_usage(response: dict) -> Usage:
        usage = response.get("usage") or {}
        return Usage(
            input_tokens=usage.get("prompt_tokens", 0) or 0,
            output_tokens=usage.get("completion_tokens", 0) or 0,
        )

    def format_tool_definitions(self, tools: list[dict]) -> list[dict]:
        """Map the protocol's tool definitions to watsonx ``tools`` (OpenAI
        function-tool shape — watsonx's chat API accepts it verbatim)."""
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
        """Append the model's tool-call turn + the tool results (OpenAI-shaped:
        an assistant message carrying ``tool_calls`` with JSON-string arguments,
        then one ``tool``-role message per result keyed by ``tool_call_id``)."""
        wx_tool_calls = [
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
            "tool_calls": wx_tool_calls,
        }
        tool_msgs = [
            {"role": "tool", "tool_call_id": tr.tool_call_id, "content": tr.content}
            for tr in tool_results
        ]
        return [*messages, assistant_msg, *tool_msgs]
