"""Provider factory — turns an agent config into a concrete LLM provider.

Split out of :mod:`agents.llm_client` (which keeps the ``LLMClient`` facade
and the re-export surface) so each file stays under the repo's size cap.
``create_provider`` is re-exported from :mod:`agents.llm_client`, so the
historical ``from agents.llm_client import create_provider`` import path is
unchanged.

This is the RFC 0033 §D integration point: the configured ``model`` field
is run through :func:`agents.model_aliases.resolve`, so an agent can name a
logical alias (``quality`` / ``fast`` / ``summarizer``) or a raw vendor ID.
Provider selection is **purely config/alias-driven** — every provider
(``anthropic`` / ``openai`` / ``gemini`` / ``watsonx`` / ``ollama`` / ``mock``)
is chosen the same standard way, by the resolved ``provider`` field. There is no global
force-knob: the keyless ``make demo-offline`` / ``make demo-ollama`` /
``make demo-openai`` paths select their provider by mounting an alias config
that points the agents' aliases at ``mock`` / ``ollama`` / ``openai`` (the
v0.3.4 provider-parity refactor removed ``PERSATRIX_OFFLINE`` /
``PERSATRIX_OLLAMA``).
"""

from __future__ import annotations

import logging
import os
from typing import Any

from .llm_gemini import GeminiProvider
from .llm_offline import MockProvider
from .llm_ollama import OllamaProvider, resolve_ollama_base_url
from .llm_providers import AnthropicProvider, OpenAIProvider
from .llm_types import LLMProvider
from .llm_watsonx import WatsonxProvider, resolve_watsonx_config
from .model_aliases import resolve as resolve_model

logger = logging.getLogger(__name__)

# Ollama speaks the OpenAI-compatible wire format, so OllamaProvider is a thin
# subclass of OpenAIProvider and shares its only hard dependency (the openai
# SDK). Surface the same actionable install hint on both Ollama entry points.
_OLLAMA_IMPORT_ERROR = (
    "Provider 'ollama' requires package 'openai' (Ollama speaks the "
    "OpenAI-compatible API). Install with: pip install 'openai>=1.50.0'"
)


def create_provider(agent_config: dict[str, Any]) -> tuple[LLMProvider, str]:
    """Create an LLM provider from agent config (RFC 0033 §D).

    Returns ``(provider_instance, physical_model_id)``. The caller threads
    the *physical* model into ``create_message(model=…)`` so the API call
    goes to the vendor ID — never an alias name.

    The ``model`` field is run through the RFC 0033 resolver and the resolved
    ``provider`` selects the concrete class — the **same standard path for
    every provider** (``anthropic`` / ``openai`` / ``gemini`` / ``watsonx`` /
    ``ollama`` / ``mock``). The
    ``model`` field must be a declared ``models.aliases`` entry: the alias is
    authoritative for the provider (an explicit, *disagreeing* ``provider:``
    field is a ``SystemExit`` — §D rule 1) and for ``provider_config``
    per-field (§D rule 2). So routing the whole society to a local / mock /
    OpenAI provider is a one-line edit to the ``quality`` alias — exactly what
    the ``make demo-*`` overlays mount. A raw vendor ID is **not** accepted:
    RFC 0033 Phase 3 retired the §E pass-through, so the resolver rejects any
    non-alias reference with a loud ``SystemExit``.

    There is no global env force-knob — provider selection is config-driven
    (the v0.3.4 provider-parity refactor removed ``PERSATRIX_OFFLINE`` /
    ``PERSATRIX_OLLAMA``).
    """
    model = agent_config["model"]
    # S-18: guard against empty model string — "" would otherwise reach the
    # resolver as an empty reference.
    if not model:
        raise SystemExit("Agent config 'model' field is empty")

    agent_id = agent_config.get("id", "<unknown>")
    explicit_provider = agent_config.get("provider")
    # The resolver rejects any non-alias reference (RFC 0033 Phase 3); on the
    # alias path the entry is authoritative and an agent-entry provider hint
    # is validated against it below, never used to route.
    resolved = resolve_model(model)

    if explicit_provider and explicit_provider != resolved.provider:
        # §D rule 1 — a model that resolves to an alias plus a disagreeing
        # explicit provider is a config bug, not a silent resolve-one-way.
        raise SystemExit(
            f"Agent {agent_id!r}: model alias {resolved.alias!r} declares "
            f"provider {resolved.provider!r}, but the agent entry sets "
            f"provider {explicit_provider!r}. The alias is authoritative "
            f"(RFC 0033 §D) — drop the redundant 'provider' field."
        )

    provider = resolved.provider
    physical_model = resolved.model
    # §D rule 2 — alias-level provider_config wins per-field; the agent
    # entry's provider_config fills only the gaps the alias leaves unset.
    provider_config = {
        **(agent_config.get("provider_config") or {}),
        **resolved.provider_config,
    }

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
            return AnthropicProvider(api_key=api_key), physical_model
        except ImportError:
            raise SystemExit(
                "Provider 'anthropic' requires package 'anthropic'. "
                "Install with: pip install 'anthropic>=0.40.0'"
            )
    elif provider == "openai":
        openai_base_url = provider_config.get("base_url")
        api_key = os.environ.get("OPENAI_API_KEY")
        # S-09: Only warn for non-local providers (base_url implies local/custom).
        if not api_key and not openai_base_url:
            logger.warning(
                "OPENAI_API_KEY not set — OpenAI provider will fail on first request"
            )
        try:
            return OpenAIProvider(
                api_key=api_key,
                base_url=openai_base_url,
            ), physical_model
        except ImportError:
            raise SystemExit(
                "Provider 'openai' requires package 'openai'. "
                "Install with: pip install 'openai>=1.50.0'"
            )
    elif provider == "gemini":
        # Native google-genai provider (RFC 0053 §B; OQ #1 → native, not the
        # OpenAI-compat endpoint). The secret key comes from GEMINI_API_KEY,
        # falling back to GOOGLE_API_KEY (the SDK's own env name). The
        # non-secret Vertex knobs (project/location) ride provider_config, the
        # same channel OpenAI's base_url uses.
        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get(
            "GOOGLE_API_KEY"
        )
        # S-09: warn (do not crash) at startup on a missing key — the provider
        # builds its client lazily, so this fails on the first request, not here.
        if not api_key:
            logger.warning(
                "GEMINI_API_KEY (or GOOGLE_API_KEY) not set — Gemini provider "
                "will fail on first request"
            )
        try:
            return GeminiProvider(
                api_key=api_key,
                provider_config=provider_config,
            ), physical_model
        except ImportError:
            raise SystemExit(
                "Provider 'gemini' requires package 'google-genai'. "
                "Install with: pip install 'google-genai>=1.0.0' "
                "(or the extra: pip install 'persatrix-agents[gemini]')"
            )
    elif provider == "watsonx":
        # Native ibm-watsonx-ai provider (RFC 0053 §C). The secret IAM key comes
        # from WATSONX_API_KEY (env). The regional `url` + `project_id` (or
        # `space_id`) are non-secret config: their source of truth is the alias
        # `provider_config` (the same channel OpenAI's base_url uses), but each
        # also accepts a WATSONX_* env fallback (resolve_watsonx_config, the
        # Ollama base_url precedent) so the demo config can ship generic and an
        # operator's project_id/region need not be committed. `url` carries a
        # us-south default; only a missing id can fail closed.
        api_key = os.environ.get("WATSONX_API_KEY")
        url, project_id, space_id = resolve_watsonx_config(provider_config)
        # Fail CLOSED on an absent project_id AND space_id — deliberately the loud
        # missing-*SDK* posture, not the softer missing-*key* warning below: the
        # client literally cannot be constructed without one, so this must surface
        # at startup, not defer to the first request. (RFC 0053 §C.) `url` always
        # resolves (default), so it is no longer part of the guard.
        if not project_id and not space_id:
            raise SystemExit(
                f"Provider 'watsonx' requires a project_id (or space_id) for "
                f"{resolved.alias!r} — set it in the alias provider_config OR the "
                "WATSONX_PROJECT_ID (or WATSONX_SPACE_ID) env (RFC 0053 §C: these "
                "are non-secret config, so either channel works — only "
                "WATSONX_API_KEY is a secret). Example: provider_config: "
                "{project_id: <id>}  — or  export WATSONX_PROJECT_ID=<id>"
            )
        # S-09: warn (do not crash) on a missing secret key — it is recoverable
        # per-request (an auth error on the first call), unlike required config.
        if not api_key:
            logger.warning(
                "WATSONX_API_KEY not set — watsonx provider will fail on first request"
            )
        try:
            return WatsonxProvider(
                api_key=api_key,
                url=url,
                project_id=project_id,
                space_id=space_id,
            ), physical_model
        except ImportError:
            raise SystemExit(
                "Provider 'watsonx' requires package 'ibm-watsonx-ai'. "
                "Install with: pip install 'ibm-watsonx-ai>=1.1.0' "
                "(or the extra: pip install 'persatrix-agents[watsonx]')"
            )
    elif provider == "ollama":
        # Local model over Ollama's OpenAI-compatible API. No API key needed
        # (Ollama ignores auth); base_url resolves provider_config first, then
        # the PERSATRIX_OLLAMA_BASE_URL env, then the localhost default. The
        # alias/agent `model:` is a real Ollama tag, used verbatim — unless
        # PERSATRIX_OLLAMA_MODEL is set, a configuration override (not a
        # selection knob) that swaps the tag for every agent built here so the
        # demo's `ollama-pull` and the agents stay in step. Scope: this is the
        # create_provider (agent) surface only — the summarisation-on-close model
        # (persona_runtime.summarize_close, RFC 0020) resolves on its own surface
        # and is not covered by this override (ISSUE-0075).
        model_override = os.environ.get("PERSATRIX_OLLAMA_MODEL", "").strip()
        ollama_model = model_override or physical_model
        try:
            return OllamaProvider(
                base_url=resolve_ollama_base_url(provider_config),
            ), ollama_model
        except ImportError:
            raise SystemExit(_OLLAMA_IMPORT_ERROR)
    elif provider == "mock":
        # Zero-cost offline provider — no API key, no network. Selected the
        # same standard way (an alias / agent entry declaring `provider: mock`),
        # so `make demo-offline` is just an alias config pointing the society
        # at the mock. MockProvider ignores the physical model, but it is
        # returned so the RFC 0023 lease keys on the same string the derived
        # cost table prices ($0 for the local mock entry).
        logger.info(
            "Offline mock provider active for agent %r — using MockProvider "
            "(no API calls, no cost)",
            agent_id,
        )
        return MockProvider.from_config(agent_config), physical_model
    raise SystemExit(f"Unknown LLM provider: {provider!r}")


__all__ = ["create_provider"]
