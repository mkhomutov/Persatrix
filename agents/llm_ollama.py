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

Activation is purely config/alias-driven — the **same standard way** every
other provider is selected (RFC 0033). There is no global force-knob:

* ``provider: ollama`` on a ``models.aliases`` entry the agents reference
  (e.g. ``quality`` → ``{provider: ollama, model: llama3.2, …}``) routes the
  whole society to the local daemon — this is how ``make demo-ollama`` works.
* ``provider: ollama`` directly on an agent's ``config/agents.yaml`` entry
  opts a single agent in.

Both flow through :func:`agents.llm_client.create_provider`'s provider
dispatch. The daemon endpoint is read from the alias/agent
``provider_config.base_url`` (most specific), then ``PERSATRIX_OLLAMA_BASE_URL``
(the deployment-wide endpoint the compose overlay sets to reach the bundled
``ollama`` service), then the localhost default. ``PERSATRIX_OLLAMA_MODEL`` is
an optional model *override* the factory applies to ollama-routed agents (it
keeps the demo's ``ollama-pull`` and the agents in lock-step). Both env vars
are provider *configuration* — analogous to an API key — not selection knobs.

**No API key.** Ollama ignores the ``Authorization`` header, but the OpenAI
SDK rejects an empty key at construction, so a harmless sentinel is passed
when the caller supplies none.

**Cost / observability.** Local inference is $0, but it is still a real
provider call: token usage comes back from Ollama's OpenAI-compatible
``usage`` block, so the OTel ``gen_ai.usage.*`` spans, token metrics, and the
RFC 0023 wallet-lease settle path are populated with real counts. The span's
``gen_ai.system`` reads ``ollama`` and ``gen_ai.request.model`` reads the
physical local tag the factory resolved.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from .llm_providers import OpenAIProvider

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_OLLAMA_BASE_URL",
    "OllamaProvider",
    "resolve_ollama_base_url",
]

_BASE_URL_ENV = "PERSATRIX_OLLAMA_BASE_URL"

# Stock local daemon, OpenAI-compatible path. Note the ``/v1`` suffix —
# Ollama's native API lives at the root, the OpenAI-compatible surface under
# ``/v1`` (so this differs from Ollama's own ``OLLAMA_HOST``, which omits it).
DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434/v1"

# Ollama never checks auth, but openai.AsyncOpenAI raises without a non-empty
# key. The Ollama docs themselves use this exact sentinel ("api_key='ollama'
# # required, but unused").
_SENTINEL_API_KEY = "ollama"


def resolve_ollama_base_url(provider_config: dict[str, Any] | None = None) -> str:
    """Resolve the daemon base URL, most-specific source first.

    Precedence: the alias/agent ``provider_config.base_url`` (a per-agent
    escape hatch) → the ``PERSATRIX_OLLAMA_BASE_URL`` env (the deployment-wide
    knob the compose overlay sets to reach the bundled ``ollama`` service) →
    the localhost default for a stock ``ollama serve``.
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
    tool formatting, response normalisation, and per-call ``model`` handling
    unchanged. Overrides only:

    * ``name = "ollama"`` so the OTel ``gen_ai.system`` attribute and the
      cost/metric attribution read ``ollama`` (a $0 local surface), not
      ``openai``.
    * a ``base_url`` defaulting to the stock local daemon so a keyless
      ``ollama serve`` needs no ``provider_config``.

    The physical model is resolved by the factory (from the alias/agent entry,
    or the ``PERSATRIX_OLLAMA_MODEL`` override) and threaded through
    ``create_message(model=…)`` like every other provider — no in-provider
    substitution.
    """

    name = "ollama"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> None:
        super().__init__(
            api_key=api_key or _SENTINEL_API_KEY,
            base_url=base_url or DEFAULT_OLLAMA_BASE_URL,
        )
