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
(``anthropic`` / ``openai`` / ``ollama`` / ``mock``) is chosen the same
standard way, by the resolved ``provider`` field. There is no global
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

from .llm_offline import MockProvider
from .llm_ollama import OllamaProvider, resolve_ollama_base_url
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

    The ``model`` field is run through the RFC 0033 resolver and the resolved
    ``provider`` selects the concrete class — the **same standard path for
    every provider** (``anthropic`` / ``openai`` / ``ollama`` / ``mock``):

    * A declared ``models.aliases`` entry is authoritative for the
      provider (an explicit, *disagreeing* ``provider:`` field is a
      ``SystemExit`` — §D rule 1) and for ``provider_config`` per-field
      (§D rule 2). So routing the whole society to a local / mock / OpenAI
      provider is a one-line edit to the ``quality`` alias — exactly what the
      ``make demo-*`` overlays mount.
    * A raw vendor ID falls through (§E): the explicit ``provider:`` field
      wins, else the provider is inferred from the prefix table; a
      one-shot deprecation warning fires and the per-agent
      ``persatrix.llm.alias.raw_id_usage`` counter increments.

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
        # Local model over Ollama's OpenAI-compatible API. No API key needed
        # (Ollama ignores auth); base_url resolves provider_config first, then
        # the PERSATRIX_OLLAMA_BASE_URL env, then the localhost default. The
        # alias/agent `model:` is a real Ollama tag, used verbatim — unless
        # PERSATRIX_OLLAMA_MODEL is set, a configuration override (not a
        # selection knob) that swaps the tag for *every* ollama-routed agent so
        # the demo's `ollama-pull` and the agents stay in lock-step.
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
