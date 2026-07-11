"""RFC 0033 PR 5 / §F — alias-derived ``cost.pricing.models``.

Split out of ``test_optimization.py`` to keep that module under the 500-line
file-size cap (same discipline as the PR 3 routing-accessor split).

The legacy ``cost.pricing.models`` block the Go cost pipeline reads is the
*projection* of the alias map: ``derived_cost_pricing()`` maps each alias's
physical model to its pricing, and the committed block must equal that
projection (the §F drift guard). This is what re-keys cost automatically on a
migration and closes the PR 3 cost regression (``quality`` →
``claude-sonnet-4-6`` is priced; the retired raw id is dropped).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from agents import optimization
from agents.optimization import reset_cache


@pytest.fixture()
def config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point ``optimization.yaml`` resolution at a per-test tmp file.

    Clears the ``_load_config`` lru_cache before AND after so a stale cache
    cannot leak across tests in either direction.
    """
    path = tmp_path / "optimization.yaml"
    monkeypatch.setenv("PERSATRIX_OPTIMIZATION_CONFIG", str(path))
    reset_cache()
    yield path
    reset_cache()


def _write_yaml(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


class TestDerivedCostPricing:
    """``derived_cost_pricing()`` projects the alias map into the legacy
    ``cost.pricing.models`` shape the Go cost pipeline reads (RFC 0033 §F).

    Each alias entry's *physical* ``model`` becomes a pricing key, so a vendor
    swap on an alias re-keys the cost table automatically — no separate edit,
    no missed entry silently mis-attributing cost.
    """

    def test_empty_alias_map_yields_empty_pricing(self, config_path: Path) -> None:
        assert optimization.derived_cost_pricing() == {}

    def test_maps_each_alias_physical_model(self, config_path: Path) -> None:
        _write_yaml(
            config_path,
            "models:\n"
            "  aliases:\n"
            "    quality:\n"
            "      provider: anthropic\n"
            "      model: claude-sonnet-4-6\n"
            "      input_per_1m_tokens: 3.00\n"
            "      output_per_1m_tokens: 15.00\n"
            "    quality-openai:\n"
            "      provider: openai\n"
            "      model: gpt-4o\n"
            "      input_per_1m_tokens: 2.50\n"
            "      output_per_1m_tokens: 10.00\n",
        )
        assert optimization.derived_cost_pricing() == {
            "claude-sonnet-4-6": {
                "input_per_1m_tokens": 3.00,
                "output_per_1m_tokens": 15.00,
            },
            "gpt-4o": {
                "input_per_1m_tokens": 2.50,
                "output_per_1m_tokens": 10.00,
            },
        }

    def test_aliases_sharing_a_physical_model_collapse(
        self, config_path: Path,
    ) -> None:
        # fast + summarizer both → Haiku; the derived table has a single key
        # carrying their (identical) price. A swap on either re-keys it.
        _write_yaml(
            config_path,
            "models:\n"
            "  aliases:\n"
            "    fast:\n"
            "      provider: anthropic\n"
            "      model: claude-haiku-4-5-20251001\n"
            "      input_per_1m_tokens: 0.80\n"
            "      output_per_1m_tokens: 4.00\n"
            "    summarizer:\n"
            "      provider: anthropic\n"
            "      model: claude-haiku-4-5-20251001\n"
            "      input_per_1m_tokens: 0.80\n"
            "      output_per_1m_tokens: 4.00\n",
        )
        assert optimization.derived_cost_pricing() == {
            "claude-haiku-4-5-20251001": {
                "input_per_1m_tokens": 0.80,
                "output_per_1m_tokens": 4.00,
            },
        }

    def test_local_zero_priced_model_is_kept(self, config_path: Path) -> None:
        # A $0-real local model carries an explicit 0 (not absent), so it is a
        # legitimate priced entry and appears in the derived table; the Go
        # EstimateCost then reads $0 for it — correct for a local model.
        _write_yaml(
            config_path,
            "models:\n"
            "  aliases:\n"
            "    local-fast:\n"
            "      provider: ollama\n"
            "      model: llama3.1\n"
            "      input_per_1m_tokens: 0\n"
            "      output_per_1m_tokens: 0\n",
        )
        assert optimization.derived_cost_pricing() == {
            "llama3.1": {"input_per_1m_tokens": 0.0, "output_per_1m_tokens": 0.0},
        }

    def test_entry_without_model_is_skipped(self, config_path: Path) -> None:
        # A malformed entry missing `model:` cannot be keyed; it is dropped
        # rather than crashing the projection (the resolver / schema reject it
        # elsewhere — this stays defensive).
        _write_yaml(
            config_path,
            "models:\n"
            "  aliases:\n"
            "    broken:\n"
            "      provider: anthropic\n"
            "      input_per_1m_tokens: 3.00\n"
            "      output_per_1m_tokens: 15.00\n",
        )
        assert optimization.derived_cost_pricing() == {}

    def test_conflicting_prices_for_shared_model_fail_loud(
        self, config_path: Path,
    ) -> None:
        # Two aliases resolving to the SAME physical model but declaring
        # DIFFERENT prices cannot both be represented in the Go cost table: it
        # keys by physical model id and telemetry carries only that id (not the
        # alias), so the table holds one price per model. Silently keeping the
        # last YAML entry (the pre-guard behaviour) would discard one alias's
        # declared price and mis-attribute its cost. The projection must fail
        # loud instead — same fail-closed discipline as the PR 4 missing-price
        # guard (RFC 0033 §F). Contrast `test_aliases_sharing_a_physical_model
        # _collapse`, where the shared prices are IDENTICAL and legitimately
        # collapse to one key.
        _write_yaml(
            config_path,
            "models:\n"
            "  aliases:\n"
            "    fast:\n"
            "      provider: anthropic\n"
            "      model: claude-haiku-4-5-20251001\n"
            "      input_per_1m_tokens: 0.80\n"
            "      output_per_1m_tokens: 4.00\n"
            "    summarizer:\n"
            "      provider: anthropic\n"
            "      model: claude-haiku-4-5-20251001\n"
            "      input_per_1m_tokens: 0.90\n"  # conflicting input price
            "      output_per_1m_tokens: 4.50\n",
        )
        with pytest.raises(SystemExit, match="different pricing"):
            optimization.derived_cost_pricing()


class TestCostPricingModelsAccessor:
    """``cost_pricing_models()`` reads the committed ``cost.pricing.models``
    block — the shape the Go cost pipeline consumes (RFC 0033 §F)."""

    def test_missing_block_returns_empty(self, config_path: Path) -> None:
        _write_yaml(config_path, "active_profile: default\n")
        assert optimization.cost_pricing_models() == {}

    def test_reads_committed_block(self, config_path: Path) -> None:
        _write_yaml(
            config_path,
            "cost:\n"
            "  pricing:\n"
            "    models:\n"
            '      "claude-sonnet-4-6":\n'
            "        input_per_1m_tokens: 3.00\n"
            "        output_per_1m_tokens: 15.00\n",
        )
        assert optimization.cost_pricing_models() == {
            "claude-sonnet-4-6": {
                "input_per_1m_tokens": 3.00,
                "output_per_1m_tokens": 15.00,
            },
        }


class TestShippedCostPricingDerivedFromAliases:
    """The committed ``cost.pricing.models`` block in the shipped config must
    equal the projection of the alias map (RFC 0033 §F drift guard).

    This is the lock-step that makes a vendor retirement a one-alias edit: a
    change to an alias's model/pricing that is not reflected in the cost block
    fails here. It also pins the PR 3 cost-regression fix — the physical model
    behind ``quality`` is priced and the retired raw id is gone.
    """

    @staticmethod
    def _shipped(
        accessor: Callable[[], dict[str, dict[str, float]]],
    ) -> dict[str, dict[str, float]]:
        # Read the *real* shipped config: drop any test override so the
        # accessor re-reads config/optimization.yaml from disk, and reset the
        # lru_cache on both sides. Restore the prior override in the finally so
        # this helper does not leak env state into sibling tests that read the
        # config without the ``config_path`` fixture (whose monkeypatch only
        # restores vars it set itself).
        prior = os.environ.pop("PERSATRIX_OPTIMIZATION_CONFIG", None)
        reset_cache()
        try:
            return accessor()
        finally:
            if prior is not None:
                os.environ["PERSATRIX_OPTIMIZATION_CONFIG"] = prior
            reset_cache()

    @staticmethod
    def _demo(
        provider: str,
        accessor: Callable[[], dict[str, dict[str, float]]],
    ) -> dict[str, dict[str, float]]:
        # Read a per-provider demo config (config/demo/<provider>/optimization.yaml)
        # — the *configured* artifacts. The base config ships UNCONFIGURED (no
        # default provider), so the priced-physical-model assertions live here.
        # Restores the prior override so this does not leak env into siblings.
        prior = os.environ.get("PERSATRIX_OPTIMIZATION_CONFIG")
        os.environ["PERSATRIX_OPTIMIZATION_CONFIG"] = str(
            Path("config") / "demo" / provider / "optimization.yaml"
        )
        reset_cache()
        try:
            return accessor()
        finally:
            if prior is not None:
                os.environ["PERSATRIX_OPTIMIZATION_CONFIG"] = prior
            else:
                os.environ.pop("PERSATRIX_OPTIMIZATION_CONFIG", None)
            reset_cache()

    def test_shipped_does_not_clobber_existing_env_override(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``_shipped`` drops the ``PERSATRIX_OPTIMIZATION_CONFIG`` override to
        read the shipped config, but must *restore* it — otherwise it leaks env
        state into sibling tests that read the config without the
        ``config_path`` fixture (whose monkeypatch only restores vars it set).
        Regression guard for the env-pop that did not restore.
        """
        monkeypatch.setenv(
            "PERSATRIX_OPTIMIZATION_CONFIG", "/nonexistent/override.yaml",
        )
        self._shipped(optimization.cost_pricing_models)
        assert (
            os.environ.get("PERSATRIX_OPTIMIZATION_CONFIG")
            == "/nonexistent/override.yaml"
        )

    def test_committed_block_matches_derived(self) -> None:
        committed = self._shipped(optimization.cost_pricing_models)
        derived = self._shipped(optimization.derived_cost_pricing)
        if not derived:
            pytest.skip("config/optimization.yaml absent in this checkout")
        assert committed == derived

    def test_base_role_aliases_ship_unconfigured(self) -> None:
        # v0.3.4 "no default provider": the shipped base config's role aliases
        # carry the `unconfigured` sentinel — provider choice is always explicit
        # (resolving one fails loud; see test_model_aliases TestUnconfiguredSentinel).
        prior = os.environ.pop("PERSATRIX_OPTIMIZATION_CONFIG", None)
        reset_cache()
        try:
            aliases = optimization.model_aliases()
        finally:
            if prior is not None:
                os.environ["PERSATRIX_OPTIMIZATION_CONFIG"] = prior
            reset_cache()
        if not aliases:
            pytest.skip("config/optimization.yaml absent in this checkout")
        for role in ("quality", "fast", "summarizer"):
            assert aliases.get(role, {}).get("provider") == "unconfigured", role

    @pytest.mark.parametrize(
        "provider", ["anthropic", "offline", "ollama", "openai", "watsonx"]
    )
    def test_demo_committed_block_matches_derived(self, provider: str) -> None:
        # Each per-provider demo config (the configured artifacts) keeps the
        # §F drift guard: its committed cost.pricing.models equals the projection
        # of its alias map.
        committed = self._demo(provider, optimization.cost_pricing_models)
        derived = self._demo(provider, optimization.derived_cost_pricing)
        assert committed == derived

    def test_anthropic_demo_prices_quality_physical_model(self) -> None:
        # The base ships `quality` UNCONFIGURED; the anthropic demo is the
        # configured artifact that prices the physical model it resolves to
        # (claude-sonnet-4-6) so the RFC 0023 gate is not $0, retired id gone.
        # This is also what the Go alias cost-attribution gate pins against
        # (internal/server/cost_alias_gate_test.go loads config/demo/anthropic).
        pricing = self._demo("anthropic", optimization.cost_pricing_models)
        assert "claude-sonnet-4-6" in pricing
        assert pricing["claude-sonnet-4-6"]["input_per_1m_tokens"] > 0
        assert pricing["claude-sonnet-4-6"]["output_per_1m_tokens"] > 0
        assert "claude-sonnet-4-20250514" not in pricing

    def test_openai_demo_prices_physical_model(self) -> None:
        # The OpenAI peer ships as a configured demo, priced so the one-line
        # swap resolves to a priced target (amendment 2026-05-24 item 2).
        pricing = self._demo("openai", optimization.cost_pricing_models)
        assert "gpt-4o" in pricing
        assert pricing["gpt-4o"]["input_per_1m_tokens"] > 0
        assert pricing["gpt-4o"]["output_per_1m_tokens"] > 0


def test_module_exports_cost_pricing_accessors() -> None:
    """Pin the PR 5 pricing-derivation surface on ``__all__``."""
    assert "derived_cost_pricing" in optimization.__all__
    assert "cost_pricing_models" in optimization.__all__
