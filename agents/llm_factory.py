"""Provider factory — turns an agent config into a concrete LLM provider.

Split out of :mod:`agents.llm_client` (which keeps the ``LLMClient`` facade
and the re-export surface) so each file stays under the repo's size cap.
``create_provider`` is re-exported from :mod:`agents.llm_client`, so the
historical ``from agents.llm_client import create_provider`` import path is
unchanged.

This is the RFC 0033 §D integration point: the configured ``model`` field
is run through :func:`agents.model_aliases.resolve`, so an agent can name a
logical alias (``quality`` / ``fast`` / ``summarizer``) or a raw vendor ID.
The two global offline / Ollama force-flags are honoured *before* the model
field, exactly as before, so the keyless ``make demo-offline`` /
``make demo-ollama`` paths are unaffected.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from .llm_offline import MockProvider, offline_mode_enabled
from .llm_ollama import (
    OllamaProvider,
    ollama_mode_enabled,
    resolve_ollama_base_url,
    resolve_ollama_model,
    warn_if_forced_base_url_override,
)
from .llm_providers import AnthropicProvider, OpenAIProvider
from .llm_types import LLMProvider
from .model_aliases import resolve as resolve_model
from .observability.metrics import try_get_instruments

logger = logging.getLogger(__name__)

# Ollama speaks the OpenAI-compatible wire format, so OllamaProvider is a thin
# subclass of OpenAIProvider and shares its only hard dependency (the openai
# SDK). Surface the same actionable install hint on both Ollama entry points.
_OLLAMA_IMPORT_ERROR = (
    "Provider 'ollama' requires package 'openai' (Ollama speaks the "
    "OpenAI-compatible API). Install with: pip install 'openai>=1.50.0'"
)


# ─── Raw-ID deprecation signal (RFC 0033 PR 2) ──────────────
#
# When an agent's ``model:`` is a raw vendor ID rather than a
# ``models.aliases`` entry, the factory keeps routing it (the §E
# pass-through) but flags it: a human-facing deprecation warning, emitted
# once per process so a society of raw agents does not spam the log, plus a
# per-agent OTEL counter. The counter (``persatrix.llm.alias.raw_id_usage``)
# reading zero across the dogfood window is the RFC 0033 Phase 3 entrance
# signal — the gate that authorises removing the pass-through and
# ``_infer_provider``. Both are de-duplicated so re-creating an agent's
# provider within a process does not double-count; the counter dedup
# records an agent only once it has actually been counted, so a
# create_provider call that runs before metrics init (instruments
# unavailable) is retried rather than silently marked counted.
#
# Scope caveat (Phase 3 gate): this counter covers only the create_provider
# (agent) surface. The summarisation-model surface
# (``persona_runtime.summarize_close``) resolves its own model and is NOT
# counted here, so a raw summarisation model is invisible to the gate. PR 3
# migrates *both* the agent configs and the summarisation field to aliases,
# so a clean zero reading is authoritative only once that migration lands.
#
# The dedup is best-effort, not synchronized: concurrent create_provider
# calls could in principle double-warn or double-count a raw agent. That is
# harmless for a deprecation signal (worst case a duplicate log line / an
# over-count by one) and agents are loaded sequentially at startup today, so
# a lock would be over-engineering.
_raw_id_warning_emitted = False
_raw_id_counted_agents: set[str] = set()


def _note_raw_id_usage(agent_id: str, model: str) -> None:
    """Record that ``agent_id`` resolved a raw vendor ``model`` (RFC 0033 §E)."""
    global _raw_id_warning_emitted
    if not _raw_id_warning_emitted:
        logger.warning(
            "DEPRECATION (RFC 0033): agent %r references a raw vendor model "
            "ID %r instead of a models.aliases entry. Migrate it to an alias "
            "in config/optimization.yaml so a vendor retirement or provider "
            "swap is a one-line edit. The raw-ID pass-through is removed in a "
            "future release (Phase 3).",
            agent_id,
            model,
        )
        _raw_id_warning_emitted = True
    if agent_id not in _raw_id_counted_agents:
        # Record the agent as counted only *after* a successful emit. If the
        # first create_provider for an agent runs before metrics init
        # (instruments unavailable), leaving the set untouched lets a later
        # call count it once instruments exist, rather than silently marking
        # it counted-forever and under-reading the Phase 3 gate.
        inst = try_get_instruments()
        if inst is not None:
            inst.alias_raw_id_usage.add(1, attributes={"agent.id": agent_id})
            _raw_id_counted_agents.add(agent_id)


def create_provider(agent_config: dict[str, Any]) -> tuple[LLMProvider, str]:
    """Create an LLM provider from agent config (RFC 0033 §D).

    Returns ``(provider_instance, physical_model_id)``. The caller threads
    the *physical* model into ``create_message(model=…)`` so the API call
    goes to the vendor ID — never an alias name.

    Two global env force-flags are checked before the ``model`` field, in
    order of precedence:

    1. ``PERSATRIX_OFFLINE`` (or ``provider: mock``) → the zero-cost
       :class:`agents.llm_offline.MockProvider` — no API key, no network.
    2. ``PERSATRIX_OLLAMA`` → :class:`agents.llm_ollama.OllamaProvider`
       pointed at a locally-served model, with the configured model
       force-substituted for the single pulled one — no API key, no cloud
       spend. Offline wins if both are set (it needs no running daemon).

    Otherwise the ``model`` field is run through the RFC 0033 resolver:

    * A declared ``models.aliases`` entry is authoritative for the
      provider (an explicit, *disagreeing* ``provider:`` field is a
      ``SystemExit`` — §D rule 1) and for ``provider_config`` per-field
      (§D rule 2).
    * A raw vendor ID falls through (§E): the explicit ``provider:`` field
      wins, else the provider is inferred from the prefix table; a
      one-shot deprecation warning fires and the per-agent
      ``persatrix.llm.alias.raw_id_usage`` counter increments.
    """
    # Offline / mock override — checked before the model field so a demo
    # config can carry a real model id (or a placeholder) without needing
    # a key. The env var is the global "make the whole society free" knob.
    # Returns the configured model verbatim (the mock ignores it); resolve()
    # is deliberately not run here so a placeholder/empty model is tolerated.
    if offline_mode_enabled() or agent_config.get("provider") == "mock":
        logger.info(
            "LLM offline mode active for agent %r — using MockProvider "
            "(no API calls, no cost)",
            agent_config.get("id", "<unknown>"),
        )
        return MockProvider.from_config(agent_config), str(agent_config.get("model") or "")

    # Local-model override — Ollama runs a real model on the operator's own
    # machine with no API key and no cloud spend. PERSATRIX_OLLAMA=1 forces
    # EVERY agent onto the local daemon (the `make demo-ollama` knob) and
    # substitutes the single pulled model for the configured one; a per-agent
    # `provider: ollama` (handled below) opts a single agent in instead.
    # Checked after offline mode, which wins if both are set — the mock needs
    # neither a network nor a running daemon.
    if ollama_mode_enabled():
        base_url = resolve_ollama_base_url(agent_config.get("provider_config"))
        force_model = resolve_ollama_model()
        warn_if_forced_base_url_override(
            agent_config.get("id", "<unknown>"), agent_config.get("provider_config")
        )
        logger.info(
            "LLM Ollama mode active for agent %r — using OllamaProvider at %s "
            "(local model %r, no cloud calls)",
            agent_config.get("id", "<unknown>"),
            base_url,
            force_model,
        )
        try:
            return OllamaProvider(base_url=base_url, force_model=force_model), force_model
        except ImportError:
            raise SystemExit(_OLLAMA_IMPORT_ERROR)

    model = agent_config["model"]
    # S-18: guard against empty model string — "" would otherwise reach the
    # resolver as an empty reference.
    if not model:
        raise SystemExit("Agent config 'model' field is empty")

    agent_id = agent_config.get("id", "<unknown>")
    explicit_provider = agent_config.get("provider")
    # On the raw pass-through, an explicit provider field wins over prefix
    # inference (§D rule 1, raw path); on the alias path the entry is
    # authoritative and the hint is ignored by resolve().
    resolved = resolve_model(model, explicit_provider=explicit_provider)

    if resolved.raw:
        _note_raw_id_usage(str(agent_id), model)
    elif explicit_provider and explicit_provider != resolved.provider:
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
    elif provider == "ollama":
        # Per-agent local model over Ollama's OpenAI-compatible API. No API
        # key needed (Ollama ignores auth); base_url resolves provider_config
        # first, then the PERSATRIX_OLLAMA_BASE_URL env, then the localhost
        # default. The agent's configured `model` (or alias-resolved model)
        # is a real Ollama tag, used verbatim — no force-substitution on this
        # opt-in path.
        try:
            return OllamaProvider(
                base_url=resolve_ollama_base_url(provider_config),
            ), physical_model
        except ImportError:
            raise SystemExit(_OLLAMA_IMPORT_ERROR)
    raise SystemExit(f"Unknown LLM provider: {provider!r}")


__all__ = ["create_provider"]
