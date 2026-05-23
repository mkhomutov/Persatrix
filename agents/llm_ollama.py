"""Local-model LLM provider backed by Ollama's OpenAI-compatible API.

A fourth :class:`~agents.llm_types.LLMProvider` alongside ``AnthropicProvider``
/ ``OpenAIProvider`` (cloud) and ``MockProvider`` (offline, see
:mod:`agents.llm_offline`). Unlike the mock it runs a **real** model — but
locally, on the operator's own machine, so there is **no API key and no
per-token cloud spend**.

**Why a thin subclass, not a new wire format.** Ollama serves the OpenAI
Chat Completions API verbatim at ``/v1/chat/completions``
(https://ollama.com/blog/openai-compatibility), so :class:`OllamaProvider`
is a thin specialisation of :class:`agents.llm_providers.OpenAIProvider`: it
inherits the request build, tool-definition formatting, and response
normalisation untouched, and overrides only what is genuinely
Ollama-specific. A separate native ``/api/chat`` client would duplicate all
of that for no functional gain. This is the provider-addition recipe from
`RFC 0033 §H <docs/rfcs/0033-model-alias-layer.md>`_ (new class implementing
the Protocol + one ``create_provider`` branch), with Ollama as that RFC's own
worked example.

Activation (either; the global env wins, mirroring offline mode):

* ``PERSATRIX_OLLAMA=1`` — global override, forces *every* agent onto the
  local daemon regardless of its configured ``model`` / ``provider``. The
  per-call model is replaced by :data:`DEFAULT_OLLAMA_MODEL` (override with
  ``PERSATRIX_OLLAMA_MODEL``) so the single pulled model also serves the
  secondary calls (summarisation, sub-agents) whose model strings come from
  ``optimization.yaml``, not the agent entry.
* ``provider: ollama`` in an agent's ``config/agents.yaml`` entry — opts a
  single agent in; its configured ``model`` is a real Ollama tag, used
  verbatim (no force-substitution).

Both are wired in :func:`agents.llm_client.create_provider`. Offline mode
(:func:`agents.llm_offline.offline_mode_enabled`) is checked first and wins
if both env vars are set — it needs neither a network nor a running daemon.

**No API key.** Ollama ignores the ``Authorization`` header, but the OpenAI
SDK rejects an empty key at construction, so a harmless sentinel is passed
when the caller supplies none.

**Cost / observability.** Local inference is $0, but it is still a real
provider call: token usage comes back from Ollama's OpenAI-compatible
``usage`` block, so the OTel ``gen_ai.usage.*`` spans, token metrics, and the
RFC 0023 wallet-lease settle path are populated with real counts. The span's
``gen_ai.system`` reads ``ollama``. In *forced* mode the span's
``gen_ai.request.model`` still reflects the caller's configured model (e.g. a
``claude-*`` id) rather than the substituted local tag — the same
configured-model-with-different-system shape offline mode already emits — so
the substitution is documented here and in the README rather than threaded
through the generic facade.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from .llm_providers import OpenAIProvider
from .llm_types import LLMResponse

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_OLLAMA_BASE_URL",
    "DEFAULT_OLLAMA_MODEL",
    "OllamaProvider",
    "ollama_mode_enabled",
    "resolve_ollama_base_url",
    "resolve_ollama_model",
]

_OLLAMA_ENV = "PERSATRIX_OLLAMA"
_MODEL_ENV = "PERSATRIX_OLLAMA_MODEL"
_BASE_URL_ENV = "PERSATRIX_OLLAMA_BASE_URL"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

# Stock local daemon, OpenAI-compatible path. Note the ``/v1`` suffix —
# Ollama's native API lives at the root, the OpenAI-compatible surface under
# ``/v1`` (so this differs from Ollama's own ``OLLAMA_HOST``, which omits it).
DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434/v1"

# Small, widely-available default so a first `make demo-ollama` pull is quick.
DEFAULT_OLLAMA_MODEL = "llama3.2"

# Ollama never checks auth, but openai.AsyncOpenAI raises without a non-empty
# key. The Ollama docs themselves use this exact sentinel ("api_key='ollama'
# # required, but unused").
_SENTINEL_API_KEY = "ollama"


def ollama_mode_enabled() -> bool:
    """Return whether ``PERSATRIX_OLLAMA`` force-selects the local provider."""
    return os.environ.get(_OLLAMA_ENV, "").strip().lower() in _TRUTHY


def resolve_ollama_model() -> str:
    """Resolve the forced-mode model: ``PERSATRIX_OLLAMA_MODEL`` or the default."""
    return os.environ.get(_MODEL_ENV, "").strip() or DEFAULT_OLLAMA_MODEL


def resolve_ollama_base_url(provider_config: dict[str, Any] | None = None) -> str:
    """Resolve the daemon base URL, most-specific source first.

    Precedence: the agent entry's ``provider_config.base_url`` (a per-agent
    escape hatch) → the ``PERSATRIX_OLLAMA_BASE_URL`` env (the deployment-wide
    knob the compose overlay sets to reach the ``ollama`` service) → the
    localhost default for a stock ``ollama serve``.
    """
    if provider_config:
        configured = provider_config.get("base_url")
        if isinstance(configured, str) and configured.strip():
            return configured.strip()
    env = os.environ.get(_BASE_URL_ENV, "").strip()
    return env or DEFAULT_OLLAMA_BASE_URL


class OllamaProvider(OpenAIProvider):
    """Local-model provider over Ollama's OpenAI-compatible API.

    Inherits :class:`agents.llm_providers.OpenAIProvider`'s request build,
    tool formatting, and response normalisation unchanged. Overrides only:

    * ``name = "ollama"`` so the OTel ``gen_ai.system`` attribute and the
      cost/metric attribution read ``ollama`` (a $0 local surface), not
      ``openai``.
    * a ``base_url`` defaulting to the stock local daemon so a keyless
      ``ollama serve`` needs no ``provider_config``.
    * an optional ``force_model``: when set (forced global mode), every call's
      model is replaced by it, so the one pulled model also serves secondary
      calls (summariser / sub-agent) whose model comes from
      ``optimization.yaml``. Unset (per-agent ``provider: ollama``) ⇒ the
      caller's model is used verbatim.
    """

    name = "ollama"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        force_model: str | None = None,
        api_key: str | None = None,
    ) -> None:
        super().__init__(
            api_key=api_key or _SENTINEL_API_KEY,
            base_url=base_url or DEFAULT_OLLAMA_BASE_URL,
        )
        self._force_model = force_model or None

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
        return await super().create_message(
            model=self._force_model or model,
            messages=messages,
            system=system,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
        )
