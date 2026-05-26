"""Lazy loader for ``config/optimization.yaml`` (RFC 0020 PR 4).

The Go orchestrator owns the optimization config end-to-end (model
routing, caching, budgets); the Python agent runtime only needs a
narrow read-side: the summarisation model selection used by the
RFC 0020 PR 4 summarisation-on-close hook in
:mod:`agents.persona_runtime.state_persistence`.

This module is deliberately minimal — one cached read, one accessor —
so the Python side does not duplicate the Go orchestrator's optimisation
schema.  Returning a sensible default on missing / malformed config
keeps tests and dev environments running without a populated YAML.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# No hardcoded model defaults live in this module: model identity is owned
# end-to-end by config/optimization.yaml (RFC 0033 — the alias map is the
# single source of truth). A code-baked default would silently re-route to
# a model the operator never chose — the exact behaviour the resolver's
# loud-fail-on-unknown design replaced. When config does not declare a
# routing key, the accessors below either return "" (summarisation — the
# best-effort close path degrades to its deterministic fallback summary) or
# fail loud (sub-agent — there is no downstream resolver to catch a bad
# value yet, so the misconfiguration must surface at construction).

# Repo-relative default location.  ``PERSATRIX_OPTIMIZATION_CONFIG`` env
# var overrides for tests / non-standard deployments.
_DEFAULT_CONFIG_PATH: Path = (
    Path(__file__).resolve().parent.parent / "config" / "optimization.yaml"
)


@lru_cache(maxsize=1)
def _load_config() -> dict[str, Any]:
    """Read and parse ``optimization.yaml`` once per process.

    On any failure (missing file, parse error, unexpected shape) returns
    an empty dict so accessors fall through to defaults.  The cache is
    process-wide; tests that mutate the file should call
    :func:`reset_cache` before re-reading.
    """
    config_path = Path(
        os.environ.get("PERSATRIX_OPTIMIZATION_CONFIG", _DEFAULT_CONFIG_PATH),
    )
    try:
        with config_path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.debug("optimization.yaml not found at %s; using defaults", config_path)
        return {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning(
            "Failed to load optimization.yaml at %s: %s; using defaults",
            config_path, exc,
        )
        return {}
    if not isinstance(data, dict):
        logger.warning(
            "optimization.yaml at %s did not parse to a dict; using defaults",
            config_path,
        )
        return {}
    return data


def reset_cache() -> None:
    """Clear the cached config (used by tests that swap in fixture files)."""
    _load_config.cache_clear()


def summarization_model() -> str:
    """Return the model reference for the RFC 0020 PR 4 summarisation-on-close hook.

    Resolution order:

    1. ``<active_profile>.context_management.summarization.model``
    2. ``default.context_management.summarization.model``
    3. ``""`` when neither is configured.

    There is **no hardcoded model fallback** (RFC 0033 — config owns model
    identity). The shipped config references the ``summarizer`` alias; when
    no model is configured the empty string flows into ``resolve()`` at the
    call site, which raises and the best-effort close path degrades to its
    deterministic fallback summary — rather than silently summarising with a
    code-baked model the operator never chose.
    """
    cfg = _load_config()
    active = cfg.get("active_profile") or "default"
    profiles = (active, "default") if active != "default" else ("default",)
    for profile in profiles:
        section = cfg.get(profile)
        if not isinstance(section, dict):
            continue
        ctx = section.get("context_management")
        if not isinstance(ctx, dict):
            continue
        summ = ctx.get("summarization")
        if not isinstance(summ, dict):
            continue
        model = summ.get("model")
        if isinstance(model, str) and model.strip():
            return model
    return ""


def provider_inference() -> dict[str, list[str]]:
    """Return the provider-inference routing rules from optimization.yaml.

    Resolution order:

    1. ``<active_profile>.model_routing.provider_inference``
    2. ``default.model_routing.provider_inference``
    3. Empty dict (caller falls back to hardcoded defaults).

    The returned dict has up to three keys:
    ``anthropic_prefixes``, ``openai_exact``, ``openai_prefixes``.
    """
    cfg = _load_config()
    active = cfg.get("active_profile") or "default"
    profiles = (active, "default") if active != "default" else ("default",)
    for profile in profiles:
        section = cfg.get(profile)
        if not isinstance(section, dict):
            continue
        routing = section.get("model_routing")
        if not isinstance(routing, dict):
            continue
        inference = routing.get("provider_inference")
        if isinstance(inference, dict):
            return {k: list(v) for k, v in inference.items() if isinstance(v, list)}
    return {}


def model_routing_defaults() -> dict[str, str]:
    """Return ``<profile>.model_routing.defaults`` — the alias each agent
    *role* (``task_agents`` / ``sub_agents`` / ``evaluators``) routes to
    when it does not name a model explicitly.

    Resolution order mirrors :func:`provider_inference`:

    1. ``<active_profile>.model_routing.defaults``
    2. ``default.model_routing.defaults``
    3. Empty dict (caller falls back to its own default).

    Values that are not strings are dropped — the resolver only ever
    resolves string references, so a list/scalar typo is defensive noise
    rather than a runtime ``TypeError`` at the first sub-agent spawn.
    """
    cfg = _load_config()
    active = cfg.get("active_profile") or "default"
    profiles = (active, "default") if active != "default" else ("default",)
    for profile in profiles:
        section = cfg.get(profile)
        if not isinstance(section, dict):
            continue
        routing = section.get("model_routing")
        if not isinstance(routing, dict):
            continue
        defaults = routing.get("defaults")
        if isinstance(defaults, dict):
            return {
                k: v
                for k, v in defaults.items()
                if isinstance(k, str) and isinstance(v, str)
            }
    return {}


def sub_agent_default_model() -> str:
    """Return the alias a code-spawned sub-agent defaults to (RFC 0033 §J.3).

    Reads ``default.model_routing.defaults.sub_agents`` via
    :func:`model_routing_defaults`. A
    :class:`~agents.persona_types.SubAgentRequest` constructed with no
    explicit ``model`` resolves to this value at construction time, so no
    Python runtime code carries a literal vendor model ID.

    There is **no hardcoded fallback** (RFC 0033 — config owns model
    identity). When the routing default is absent this raises a loud
    :class:`SystemExit` naming the missing key: a sub-agent cannot run
    without a model, and unlike the summarisation surface there is no
    downstream resolver to catch a placeholder value, so the
    misconfiguration must surface at construction rather than route to a
    code-baked default.
    """
    alias = model_routing_defaults().get("sub_agents")
    if not alias:
        raise SystemExit(
            "config/optimization.yaml: default.model_routing.defaults.sub_agents "
            "is not set — a code-spawned sub-agent with no explicit model has no "
            "alias to route to. Declare it in the routing defaults (e.g. "
            "sub_agents: quality)."
        )
    return alias


def model_aliases() -> dict[str, dict[str, Any]]:
    """Return the ``models.aliases`` block from optimization.yaml.

    The RFC 0033 alias map is the single source of truth for model
    identity: each entry maps a logical alias (``quality`` / ``fast`` /
    ``summarizer``) to a concrete ``(provider, model, pricing)`` record.
    Unlike :func:`provider_inference`, the block is **not** profile-scoped
    — it sits at the top level alongside ``default`` / ``cost`` (RFC 0033
    §B), so resolution does not consult ``active_profile``.

    Returns a fresh copy of the block (outer dict + each entry) so a
    caller mutating the result cannot poison the :func:`_load_config`
    lru_cache.  Non-dict entries are dropped — a scalar where an alias
    entry should be is a config typo the resolver should not choke on.
    Missing / malformed config yields an empty dict; the consumer
    (:mod:`agents.model_aliases`) treats an absent map as "no aliases
    declared, every reference is a raw vendor ID".
    """
    cfg = _load_config()
    models = cfg.get("models")
    if not isinstance(models, dict):
        return {}
    aliases = models.get("aliases")
    if not isinstance(aliases, dict):
        return {}
    return {
        name: dict(entry)
        for name, entry in aliases.items()
        if isinstance(entry, dict)
    }


__all__ = [
    "model_aliases",
    "model_routing_defaults",
    "provider_inference",
    "reset_cache",
    "sub_agent_default_model",
    "summarization_model",
]
