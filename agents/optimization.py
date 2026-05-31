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


def model_routing_defaults() -> dict[str, str]:
    """Return ``<profile>.model_routing.defaults`` — the alias each agent
    *role* (``task_agents`` / ``sub_agents`` / ``evaluators``) routes to
    when it does not name a model explicitly.

    Resolution order (active profile, then ``default``):

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
    Unlike :func:`model_routing_defaults`, the block is **not** profile-scoped
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


def _as_price(value: Any) -> float:
    """Coerce a pricing field to ``float``.

    Mirrors :func:`agents.model_aliases._coerce_price`: a ``bool`` (an int
    subclass — a stray YAML ``true`` / ``false`` is not a price) and any
    non-numeric value read as ``0.0``. An explicit ``0`` is a real (local /
    simulation) price and survives. The PR 4 missing-price guard already
    fails closed on an unpriced *non-local* alias, so this is the defensive
    floor below that.
    """
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def derived_cost_pricing() -> dict[str, dict[str, float]]:
    """Project the alias map into the legacy ``cost.pricing.models`` shape
    the Go cost pipeline reads (RFC 0033 §F, PR 5).

    Each alias entry's *physical* ``model`` becomes a pricing key, mapping to
    its ``input_per_1m_tokens`` / ``output_per_1m_tokens``. Because the Go
    orchestrator keys ``EstimateCost`` (``internal/cost/config.go``) by the
    physical model id it reads off telemetry, generating that table from the
    alias map keeps pricing in lock-step automatically: a vendor swap on an
    alias re-keys the cost table with no separate edit and no missed entry
    silently mis-attributing cost.

    Several aliases may resolve to the same physical model (e.g. ``fast`` and
    ``summarizer`` → Haiku); when their prices are *identical* they collapse to
    one key. Two aliases sharing a physical model but declaring *different*
    prices is a config error this projection cannot represent — the Go table
    keys by physical model id and telemetry carries only that id, not the alias,
    so the table holds exactly one price per model. Rather than silently keep
    whichever entry comes last in YAML order (discarding the other alias's price
    and mis-attributing its cost), that fails loud with a :class:`SystemExit`
    naming both aliases — the same fail-closed discipline as the PR 4 missing-
    price guard. An entry missing ``model:`` cannot be keyed and is skipped —
    the resolver and JSON schema reject it elsewhere; this stays defensive.

    The committed ``cost.pricing.models`` block in ``config/optimization.yaml``
    is this projection — :func:`cost_pricing_models` reads the committed block
    and the optimization test suite asserts the two are equal (the §F drift
    guard). The block is regenerated from here when an alias's model or price
    changes, rather than hand-maintained.
    """
    pricing: dict[str, dict[str, float]] = {}
    source_alias: dict[str, str] = {}  # physical model → first alias that priced it
    for alias, entry in model_aliases().items():
        model = entry.get("model")
        if not isinstance(model, str) or not model:
            continue
        priced = {
            "input_per_1m_tokens": _as_price(entry.get("input_per_1m_tokens")),
            "output_per_1m_tokens": _as_price(entry.get("output_per_1m_tokens")),
        }
        existing = pricing.get(model)
        if existing is not None and existing != priced:
            raise SystemExit(
                f"Model aliases {source_alias[model]!r} and {alias!r} both "
                f"resolve to physical model {model!r} but declare different "
                f"pricing ({existing} vs {priced}). The Go cost table keys by "
                f"physical model id — telemetry carries only the physical id, "
                f"not the alias — so it cannot hold two prices for one model. "
                f"Give both aliases the same price, or point them at distinct "
                f"models. See RFC 0033 §F.",
            )
        pricing[model] = priced
        source_alias.setdefault(model, alias)
    return pricing


def cost_pricing_models() -> dict[str, dict[str, float]]:
    """Return the committed ``cost.pricing.models`` block from optimization.yaml.

    This is the legacy pricing table the Go cost pipeline consumes (keyed by
    physical model id). Under RFC 0033 §F it is the projection of the alias
    map — see :func:`derived_cost_pricing` — and the optimization test suite
    pins the two equal so a drift between an alias's pricing and the cost
    block fails loudly. Unlike :func:`model_aliases` this is not profile-
    scoped: ``cost`` sits at the top level. Missing / malformed config yields
    an empty dict.
    """
    cfg = _load_config()
    cost = cfg.get("cost")
    if not isinstance(cost, dict):
        return {}
    pricing = cost.get("pricing")
    if not isinstance(pricing, dict):
        return {}
    models = pricing.get("models")
    if not isinstance(models, dict):
        return {}
    result: dict[str, dict[str, float]] = {}
    for model, entry in models.items():
        if not isinstance(model, str) or not isinstance(entry, dict):
            continue
        result[model] = {
            "input_per_1m_tokens": _as_price(entry.get("input_per_1m_tokens")),
            "output_per_1m_tokens": _as_price(entry.get("output_per_1m_tokens")),
        }
    return result


__all__ = [
    "cost_pricing_models",
    "derived_cost_pricing",
    "model_aliases",
    "model_routing_defaults",
    "reset_cache",
    "sub_agent_default_model",
    "summarization_model",
]
