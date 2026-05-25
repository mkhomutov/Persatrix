"""Provider-agnostic model alias resolver (RFC 0033 Phase 1).

A single source of truth for model identity. :func:`resolve` maps a
logical alias (``quality`` / ``fast`` / ``summarizer``) to a concrete
:class:`ResolvedModel` — ``(provider, model, pricing)`` — so a vendor
retirement or a provider swap is a one-line edit to one map entry instead
of a sweep across ``config/agents.yaml``, ``config/optimization.yaml``,
``agents/persona_types.py``, and the pricing table.

During the cutover window the resolver also accepts a **raw vendor model
ID** (e.g. ``claude-sonnet-4-20250514``) and passes it through unchanged,
inferring the provider from the existing prefix table (RFC 0033 §E). A
string that is neither a declared alias nor a recognised vendor prefix is
a loud :class:`SystemExit` — the "clear up-front error" this RFC
substitutes for ``_infer_provider``'s silent default-to-openai.

This module is a **leaf**: it imports config accessors from
:mod:`agents.optimization` only — never the provider classes or
:class:`~agents.llm_client.LLMClient`. That keeps it unit-testable in
isolation and, critically, avoids an import cycle with
:mod:`agents.llm_client`, which imports *this* module once the factory is
rewired (RFC 0033 PR 2). PR 1 ships the resolver **unconsumed** — no call
site reads it yet, so it changes no runtime behaviour.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from agents.optimization import model_aliases as _load_alias_block
from agents.optimization import provider_inference

logger = logging.getLogger(__name__)

# Fallback prefix table for the raw-ID pass-through, used only when
# config/optimization.yaml ships no ``provider_inference`` block. Mirrors
# ``agents.llm_client._OPENAI_EXACT_MODELS`` / ``_OPENAI_PREFIX_MODELS``;
# tests/unit/python/test_optimization.py pins the shipped YAML to those
# constants, so the config remains the single source of truth and these
# only bind in a config-less dev checkout. They are not imported from
# ``llm_client`` because that module imports *this* one in PR 2 (cycle).
_DEFAULT_ANTHROPIC_PREFIXES: tuple[str, ...] = ("claude",)
_DEFAULT_OPENAI_EXACT: frozenset[str] = frozenset({"o1", "o3", "o4"})
_DEFAULT_OPENAI_PREFIXES: tuple[str, ...] = ("gpt-", "o1-", "o3-", "o4-")


@dataclass(frozen=True)
class ResolvedModel:
    """The resolved identity of a model reference (RFC 0033 §C).

    ``alias`` is the logical name the reference came in as, or ``None``
    when the reference was a raw vendor ID that fell through (``raw`` is
    then ``True``). ``model`` is always the physical vendor ID the API
    call must use — never the alias name.
    """

    alias: str | None
    provider: str
    model: str
    input_per_1m_tokens: float
    output_per_1m_tokens: float
    provider_config: dict[str, Any] = field(default_factory=dict)
    raw: bool = False


# ─── Test seam ────────────────────────────────────────────────
# The resolver reads its alias map from the lazy, process-wide
# optimization.yaml cache. ``use_alias_map`` lets a test register a
# temporary map for the duration of a ``with`` block without touching
# that cache, so unit tests stay hermetic.
_override_map: dict[str, dict[str, Any]] | None = None


@contextlib.contextmanager
def use_alias_map(mapping: dict[str, dict[str, Any]]) -> Iterator[None]:
    """Temporarily route :func:`resolve` through ``mapping`` (test seam).

    Restores the previously-active map (usually the config-backed
    singleton) on exit, including when the block raises, and never
    mutates the :mod:`agents.optimization` cache. Nested blocks restore
    in LIFO order.
    """
    global _override_map
    previous = _override_map
    _override_map = mapping
    try:
        yield
    finally:
        _override_map = previous


def _current_alias_map() -> dict[str, dict[str, Any]]:
    """Return the active alias map — the seam override if one is set,
    otherwise the config-backed block from :mod:`agents.optimization`."""
    if _override_map is not None:
        return _override_map
    return _load_alias_block()


def _coerce_price(value: Any) -> float:
    """Coerce a pricing field to ``float``. Absent / non-numeric pricing
    degrades to ``0.0`` — the schema requires pricing on every alias
    entry, and PR 4 adds the loud fail-closed guard for an unpriced
    non-local alias; this is the defensive floor below that."""
    if isinstance(value, bool):  # bool is an int subclass — exclude it
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def _from_alias_entry(alias: str, entry: dict[str, Any]) -> ResolvedModel:
    provider = entry.get("provider")
    if not isinstance(provider, str) or not provider:
        raise SystemExit(f"Model alias {alias!r} is missing a 'provider' field")
    model = entry.get("model")
    if not isinstance(model, str) or not model:
        raise SystemExit(f"Model alias {alias!r} is missing a 'model' field")
    provider_config = entry.get("provider_config") or {}
    if not isinstance(provider_config, dict):
        raise SystemExit(
            f"Model alias {alias!r} 'provider_config' must be a mapping",
        )
    return ResolvedModel(
        alias=alias,
        provider=provider,
        model=model,
        input_per_1m_tokens=_coerce_price(entry.get("input_per_1m_tokens")),
        output_per_1m_tokens=_coerce_price(entry.get("output_per_1m_tokens")),
        provider_config=dict(provider_config),
        raw=False,
    )


def _infer_raw_provider(model: str) -> str:
    """Infer the provider for a raw vendor model ID from the prefix table
    (RFC 0033 §E). Raises :class:`SystemExit` for a string that matches no
    known prefix — that is neither an alias nor a recognised vendor ID."""
    rules = provider_inference()
    anthropic_prefixes = tuple(
        rules.get("anthropic_prefixes", _DEFAULT_ANTHROPIC_PREFIXES),
    )
    openai_exact = frozenset(rules.get("openai_exact", _DEFAULT_OPENAI_EXACT))
    openai_prefixes = tuple(rules.get("openai_prefixes", _DEFAULT_OPENAI_PREFIXES))
    if model.startswith(anthropic_prefixes):
        return "anthropic"
    if model in openai_exact or model.startswith(openai_prefixes):
        return "openai"
    raise SystemExit(
        f"Unknown model reference {model!r}: not a declared alias in "
        f"models.aliases and not a recognised vendor model ID. Declare it "
        f"as an alias in config/optimization.yaml or use a known vendor ID.",
    )


def resolve(
    alias_or_model: str, *, explicit_provider: str | None = None,
) -> ResolvedModel:
    """Resolve ``alias_or_model`` to a :class:`ResolvedModel`.

    * A declared alias returns its configured record (``raw=False``).
      ``explicit_provider`` is ignored here — the alias entry is the joint
      declaration of provider + model + pricing and stays authoritative
      (RFC 0033 §D rule 1; a *disagreeing* explicit provider is caught by
      the factory, not silently overridden here).
    * A recognised raw vendor ID falls through with ``alias=None,
      raw=True``. When ``explicit_provider`` is given it wins over prefix
      inference (§D rule 1, raw path — preserves today's
      ``agent_config.get("provider") or _infer_provider(model)`` so a
      per-agent ``provider: ollama`` on a local tag like ``llama3.2`` that
      no prefix rule recognises still resolves). Otherwise the provider is
      inferred from the prefix table (§E). The raw record carries no
      pricing (the Go cost path keys off telemetry, §F).
    * Anything else is a loud :class:`SystemExit` naming the string.
    """
    if not alias_or_model or not alias_or_model.strip():
        raise SystemExit("Model reference is empty")

    entry = _current_alias_map().get(alias_or_model)
    if entry is not None:
        return _from_alias_entry(alias_or_model, entry)

    provider = explicit_provider or _infer_raw_provider(alias_or_model)
    return ResolvedModel(
        alias=None,
        provider=provider,
        model=alias_or_model,
        input_per_1m_tokens=0.0,
        output_per_1m_tokens=0.0,
        provider_config={},
        raw=True,
    )


__all__ = ["ResolvedModel", "resolve", "use_alias_map"]
