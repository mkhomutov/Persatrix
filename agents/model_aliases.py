"""Provider-agnostic model alias resolver (RFC 0033 Phase 1).

A single source of truth for model identity. :func:`resolve` maps a
logical alias (``quality`` / ``fast`` / ``summarizer``) to a concrete
:class:`ResolvedModel` — ``(provider, model, pricing)`` — so a vendor
retirement or a provider swap is a one-line edit to one map entry instead
of a sweep across ``config/agents.yaml``, ``config/optimization.yaml``,
``agents/persona_types.py``, and the pricing table.

As of RFC 0033 **Phase 3** the cutover-window raw-vendor-ID pass-through
(§E) is **retired**: a model reference must be a declared alias. Any string
that is not an alias — a raw vendor ID like ``claude-sonnet-4-20250514``, or
a typo'd alias name — is a loud :class:`SystemExit` naming the string and
pointing at ``models.aliases`` (the "clear up-front error" this RFC
substitutes for ``_infer_provider``'s silent default-to-openai). There is no
longer a ``raw=True`` record, no prefix inference, and no provider-hint
escape hatch.

This module is a **leaf**: it imports config accessors from
:mod:`agents.optimization` only — never the provider classes or
:class:`~agents.llm_client.LLMClient`. That keeps it unit-testable in
isolation and, critically, avoids an import cycle with
:mod:`agents.llm_client`, which imports *this* module via the factory.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from agents.optimization import model_aliases as _load_alias_block

logger = logging.getLogger(__name__)

# Local providers run on the operator's own machine at $0 real cost, so a
# $0 (or absent) price on their alias entries is by design — the
# missing-price guard (RFC 0033 PR 4) exempts them. `mock` / `ollama` are
# local by provider name; an OpenAI-compatible provider whose base_url
# points at a loopback host (e.g. a local vLLM / LM Studio server) is local
# by endpoint.
# The shipped base config ships its role aliases (quality / fast / summarizer)
# with this sentinel provider — *no provider is configured by default*. An agent
# that resolves such an alias fails loud (below) until an operator picks a
# provider (run a `make demo-*`, or set provider/model on the alias). This is the
# v0.3.4 "no default provider" stance: provider choice is always explicit.
_UNCONFIGURED_PROVIDER = "unconfigured"

_LOCAL_PROVIDERS: frozenset[str] = frozenset({"mock", "ollama"})
_LOCALHOST_HOSTS: frozenset[str] = frozenset(
    {"localhost", "127.0.0.1", "0.0.0.0", "::1"},
)


@dataclass(frozen=True)
class ResolvedModel:
    """The resolved identity of a model reference (RFC 0033 §C).

    ``alias`` is the logical name the reference came in as. As of RFC 0033
    Phase 3 every resolved reference is a declared alias (the §E raw-vendor-ID
    pass-through is retired), so ``alias`` is always set — the ``str | None``
    type is kept for the public contract. ``model`` is always the physical
    vendor ID the API call must use — never the alias name.
    """

    alias: str | None
    provider: str
    model: str
    input_per_1m_tokens: float
    output_per_1m_tokens: float
    provider_config: dict[str, Any] = field(default_factory=dict)


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
    otherwise the config-backed block from :mod:`agents.optimization`.

    The missing-price guard (RFC 0033 PR 4) is applied **per resolve**, in
    :func:`resolve`, scoped to the entry actually being resolved — *not*
    here over the whole map. Validating the whole map on every access would
    make resolving a well-formed alias fail closed because of an unrelated,
    unused misconfigured alias elsewhere (and, via
    :func:`~agents.persona_runtime.summarize_close`'s ``except SystemExit``
    safety net, silently degrade that surface). The RFC acceptance is scoped
    to the *resolved* alias; :func:`validate_alias_pricing` stays available
    for an explicit whole-map (startup / CI) check.
    """
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
    )


def _provider_is_local(entry: dict[str, Any]) -> bool:
    """Whether an alias entry routes to a local ($0-real) provider.

    Local by provider name (``mock`` / ``ollama``) or by endpoint — an
    OpenAI-compatible ``provider_config.base_url`` resolving to a *loopback*
    host (``_LOCALHOST_HOSTS``). A local model legitimately runs at $0, so the
    missing-price guard treats it as $0-by-design rather than as a forgotten
    price.

    Locality is **loopback-only** by design. A self-hosted model reached over
    a LAN/remote address (a private IP or a hostname) is *not* auto-detected
    here — the operator marks such a box local with an explicit ``0`` price
    (the fail-closed message documents that escape hatch). This guard is also
    deliberately *more lenient* than the JSON schema, which requires explicit
    pricing (a ``0`` for a local model) on **every** entry so PR 5's cost-table
    derivation stays complete: the schema is the static ``make validate`` / CI
    check, this guard is the runtime backstop for the non-local budget hole,
    and they converge on any schema-valid config (a local entry then carries
    its explicit 0, which this function never needs to inspect).
    """
    provider = entry.get("provider")
    if isinstance(provider, str) and provider.lower() in _LOCAL_PROVIDERS:
        return True
    provider_config = entry.get("provider_config")
    if isinstance(provider_config, dict):
        base_url = provider_config.get("base_url")
        if isinstance(base_url, str) and base_url:
            host = (urlparse(base_url).hostname or "").lower()
            if host in _LOCALHOST_HOSTS:
                return True
    return False


def _has_numeric_price(entry: dict[str, Any], key: str) -> bool:
    """Whether ``key`` is present on ``entry`` as a real numeric price.

    A missing key, ``None``, a non-numeric value, or a ``bool`` (an int
    subclass — a stray YAML ``true`` / ``false`` is not a price) all read
    as *no price*. An explicit ``0`` is a real (simulation) price and
    passes. This is the same numeric/bool discipline as :func:`_coerce_price`,
    used here to tell an explicit-0 entry from an unpriced one.
    """
    value = entry.get(key)
    if isinstance(value, bool):
        return False
    return isinstance(value, (int, float))


def _check_entry_pricing(alias: str, entry: dict[str, Any]) -> None:
    """Fail closed on a single unpriced **non-local** alias entry (RFC 0033 PR 4).

    The RFC 0023 budget gate goes silent when a *physical model* is absent
    from the Go cost table: ``EstimateCost`` returns ``$0`` for any model it
    does not know (``cost.pricing.models``, ``internal/cost/config.go``), and
    the pre-call lease keys off that $0, so the cap never accrues. PR 5
    (RFC 0033 §F) makes that table *derived from this alias map* — so a
    non-local alias with no inline price would derive an absent row → $0 → a
    silently-disabled gate. This guard enforces the precondition PR 5 relies
    on, **ahead of that consumer**: an unpriced non-local entry is a loud
    :class:`SystemExit` naming the alias and provider. It guarantees the
    inline price is *present*; it does **not** itself re-key the Go cost table
    — deriving that table from the alias map is PR 5's job (RFC 0033 §F). So a
    priced alias passing this guard is necessary, but not yet sufficient, for a
    live budget gate until PR 5 lands: in the interim the stock cost table is
    still keyed to the retired physical ID, so the gate reads $0 even for a
    priced alias (the PR 3 cost-regression row in the RFC 0033 PR plan). A
    local ($0-by-design) entry is exempt (:func:`_provider_is_local`),
    so an explicit-0 simulation price and a forgotten cloud price are
    distinguishable. Since RFC 0033 Phase 3 retired the §E raw-vendor-ID
    pass-through, the alias surface is the *only* surface — every resolved
    reference is a declared alias and runs through this guard.
    """
    if _provider_is_local(entry):
        return
    if _has_numeric_price(entry, "input_per_1m_tokens") and _has_numeric_price(
        entry, "output_per_1m_tokens",
    ):
        return
    provider = entry.get("provider")
    logger.error(
        "Model alias %r (provider %r) has no usable pricing — a non-local "
        "provider with no price makes cost estimation return $0, silently "
        "disabling the RFC 0023 budget/lease gate for that agent.",
        alias,
        provider,
    )
    raise SystemExit(
        f"Model alias {alias!r} (provider {provider!r}) is missing pricing "
        f"(input_per_1m_tokens / output_per_1m_tokens). A non-local provider "
        f"with no price would make cost estimation return $0, silently "
        f"disabling the RFC 0023 budget/lease gate. Add explicit pricing "
        f"(use 0 only for a $0-real local model). See RFC 0033 §F / "
        f"the v0.3.4 provider-parity amendment (item 1).",
    )


def _check_alias_pricing(mapping: dict[str, dict[str, Any]]) -> None:
    """Run :func:`_check_entry_pricing` over **every** entry in ``mapping``.

    The whole-map validator behind :func:`validate_alias_pricing` — used for
    an explicit startup / CI check that the *entire* config is priced. Raises
    :class:`SystemExit` on the first unpriced non-local entry. Per-resolve
    fail-closed (the runtime safety property) is enforced separately in
    :func:`resolve`, scoped to the resolved alias, so an unused misconfigured
    entry here cannot break resolution of a well-formed one.
    """
    for alias, entry in mapping.items():
        if not isinstance(entry, dict):
            # The accessor drops non-dict entries; a seam map might not.
            continue
        _check_entry_pricing(alias, entry)


def validate_alias_pricing(
    mapping: dict[str, dict[str, Any]] | None = None,
) -> None:
    """Run the missing-price guard over an alias map (RFC 0033 PR 4).

    With no argument, validates the currently-active map — the seam
    override if one is set, else the config-backed block — so a test can
    drive the guard explicitly inside a :func:`use_alias_map` block, and a
    caller can validate the shipped config at startup. Raises
    :class:`SystemExit` on the first unpriced non-local alias; a clean map
    returns ``None``. See :func:`_check_alias_pricing`.
    """
    if mapping is None:
        mapping = _current_alias_map()
    _check_alias_pricing(mapping)


def resolve(alias_or_model: str) -> ResolvedModel:
    """Resolve ``alias_or_model`` to a :class:`ResolvedModel`.

    * A declared alias returns its configured record. The
      alias entry is the joint declaration of provider + model + pricing and
      stays authoritative (RFC 0033 §D rule 1; a *disagreeing* explicit
      provider on an agent entry is caught by the factory, not here). A
      config-backed alias is run through the missing-price guard (RFC 0033
      PR 4), scoped to *this* entry: an unpriced non-local alias fails closed
      with a loud :class:`SystemExit`, enforcing the inline-price invariant
      the cost-table derivation keys the RFC 0023 budget gate from (see
      :func:`_check_entry_pricing`).
    * Anything else — a raw vendor ID or a typo'd alias name — is a loud
      :class:`SystemExit` naming the string. RFC 0033 **Phase 3** retired the
      §E raw-vendor-ID pass-through: there is no prefix inference, no
      ``raw=True`` record, and no provider-hint escape hatch. The single
      source of truth for model identity is ``models.aliases``.
    """
    if not alias_or_model or not alias_or_model.strip():
        raise SystemExit("Model reference is empty")

    entry = _current_alias_map().get(alias_or_model)
    if entry is not None:
        # v0.3.4 "no default provider" — the shipped base config ships role
        # aliases with `provider: unconfigured`. Resolving one means the
        # operator has not picked a provider yet: fail loud with an actionable
        # message rather than routing or returning a silent $0 record. Checked
        # *before* the price guard and unconditionally (even under the test
        # seam), so it can never be a $0 budget hole.
        if entry.get("provider") == _UNCONFIGURED_PROVIDER:
            raise SystemExit(
                f"Model alias {alias_or_model!r} is not configured — no "
                f"provider is selected (the shipped default). Pick one: run "
                f"`make demo-anthropic` / `demo-openai` / `demo-ollama` / "
                f"`demo-offline`, or set 'provider' and 'model' on the "
                f"{alias_or_model!r} alias in config/optimization.yaml. See "
                f"docs/guides/model-providers.md.",
            )
        # RFC 0033 PR 4 — fail closed if *this* resolved alias is an unpriced
        # non-local entry (a silent $0 budget gate). Scoped to the resolved
        # alias: an unrelated misconfigured alias elsewhere in the map must not
        # break this resolve. The seam override is exempt (``_override_map``
        # set ⇒ the map above is the override) — tests drive the guard
        # explicitly via :func:`validate_alias_pricing`, so a deliberately
        # broken fixture cannot derail unrelated resolver tests.
        if _override_map is None:
            _check_entry_pricing(alias_or_model, entry)
        return _from_alias_entry(alias_or_model, entry)

    # RFC 0033 Phase 3 — the §E raw-vendor-ID pass-through is retired. A
    # reference that is not a declared alias is a config error, not a silent
    # route: fail loud, naming the string and the one place to declare it.
    raise SystemExit(
        f"Unknown model reference {alias_or_model!r}: not a declared alias in "
        f"models.aliases. Declare it as an alias in config/optimization.yaml "
        f"(the raw vendor-ID pass-through was removed in RFC 0033 Phase 3).",
    )


__all__ = ["ResolvedModel", "resolve", "use_alias_map", "validate_alias_pricing"]
