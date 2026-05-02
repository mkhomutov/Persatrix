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

# Default summarisation model — matches the value shipped in
# ``config/optimization.yaml`` so missing / unreadable config produces
# the same behaviour as the on-disk default.
_DEFAULT_SUMMARIZATION_MODEL: str = "claude-haiku-4-5-20251001"

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
    """Return the model used by the RFC 0020 PR 4 summarisation-on-close hook.

    Resolution order:

    1. ``<active_profile>.context_management.summarization.model``
    2. ``default.context_management.summarization.model``
    3. :data:`_DEFAULT_SUMMARIZATION_MODEL` fallback.
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
    return _DEFAULT_SUMMARIZATION_MODEL


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


__all__ = ["provider_inference", "reset_cache", "summarization_model"]
