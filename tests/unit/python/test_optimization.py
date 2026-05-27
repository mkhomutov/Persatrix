"""Tests for the ``agents.optimization`` config accessors.

Covers ``provider_inference()`` end-to-end — the existing
``test_llm_client.py::TestCreateProvider`` cases only verify that the
shipped YAML defaults still resolve the right provider, and would pass
trivially even if the YAML diverged from the hardcoded constants in
:mod:`agents.llm_client`.  These tests pin the resolution chain itself
(active profile → default → empty dict, malformed-value filtering,
cache invalidation via :func:`reset_cache`) so a regression in the
fallback logic surfaces here rather than as a runtime model-routing
mistake.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from agents import optimization
from agents.optimization import model_aliases, provider_inference, reset_cache


@pytest.fixture()
def config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point ``optimization.yaml`` resolution at a per-test tmp file.

    Clears the ``_load_config`` lru_cache before AND after the test so a
    stale cache cannot leak across tests in either direction.
    """
    path = tmp_path / "optimization.yaml"
    monkeypatch.setenv("PERSATRIX_OPTIMIZATION_CONFIG", str(path))
    reset_cache()
    yield path
    reset_cache()


def _write_yaml(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


class TestProviderInferenceFallback:
    def test_missing_file_returns_empty_dict(self, config_path: Path) -> None:
        # File does not exist → ``_load_config`` returns ``{}`` →
        # provider_inference returns ``{}`` → ``_infer_provider`` keeps
        # using its hardcoded constants.
        assert provider_inference() == {}

    def test_no_profiles_returns_empty_dict(self, config_path: Path) -> None:
        _write_yaml(config_path, "active_profile: default\n")
        assert provider_inference() == {}

    def test_profile_without_model_routing_returns_empty_dict(
        self, config_path: Path,
    ) -> None:
        _write_yaml(
            config_path,
            "default:\n"
            "  context_management:\n"
            "    summarization:\n"
            "      model: claude-haiku-4-5-20251001\n",
        )
        assert provider_inference() == {}

    def test_routing_without_provider_inference_returns_empty_dict(
        self, config_path: Path,
    ) -> None:
        _write_yaml(
            config_path,
            "default:\n"
            "  model_routing:\n"
            "    something_else: []\n",
        )
        assert provider_inference() == {}


class TestProviderInferenceResolution:
    def test_default_profile_resolves(self, config_path: Path) -> None:
        _write_yaml(
            config_path,
            "default:\n"
            "  model_routing:\n"
            "    provider_inference:\n"
            "      anthropic_prefixes: [claude]\n"
            "      openai_exact: [o1, o3, o4]\n"
            "      openai_prefixes: [gpt-, o1-, o3-, o4-]\n",
        )
        assert provider_inference() == {
            "anthropic_prefixes": ["claude"],
            "openai_exact": ["o1", "o3", "o4"],
            "openai_prefixes": ["gpt-", "o1-", "o3-", "o4-"],
        }

    def test_active_profile_overrides_default(self, config_path: Path) -> None:
        # When ``active_profile`` is set, its rules win.  ``default`` is
        # only consulted if the active profile is absent or partial at
        # the *section* level — not for key-level fallback within the
        # provider_inference dict (current resolution semantics).
        _write_yaml(
            config_path,
            "active_profile: experimental\n"
            "default:\n"
            "  model_routing:\n"
            "    provider_inference:\n"
            "      anthropic_prefixes: [claude]\n"
            "experimental:\n"
            "  model_routing:\n"
            "    provider_inference:\n"
            "      anthropic_prefixes: [claude, anthropic-]\n",
        )
        assert provider_inference() == {
            "anthropic_prefixes": ["claude", "anthropic-"],
        }

    def test_active_profile_missing_falls_through_to_default(
        self, config_path: Path,
    ) -> None:
        _write_yaml(
            config_path,
            "active_profile: experimental\n"
            "default:\n"
            "  model_routing:\n"
            "    provider_inference:\n"
            "      anthropic_prefixes: [claude]\n",
        )
        assert provider_inference() == {"anthropic_prefixes": ["claude"]}

    def test_partial_override_returns_only_provided_keys(
        self, config_path: Path,
    ) -> None:
        # Only ``anthropic_prefixes`` is set — caller (``_infer_provider``)
        # is responsible for falling back to ``_OPENAI_EXACT_MODELS`` /
        # ``_OPENAI_PREFIX_MODELS`` for the missing keys.  This test pins
        # the contract that ``provider_inference`` itself does NOT inject
        # the hardcoded defaults — the caller must.
        _write_yaml(
            config_path,
            "default:\n"
            "  model_routing:\n"
            "    provider_inference:\n"
            "      anthropic_prefixes: [claude]\n",
        )
        result = provider_inference()
        assert result == {"anthropic_prefixes": ["claude"]}
        assert "openai_exact" not in result
        assert "openai_prefixes" not in result


class TestProviderInferenceMalformed:
    def test_non_list_values_are_filtered(self, config_path: Path) -> None:
        # ``provider_inference`` silently drops any key whose value is
        # not a list — defensive against a YAML edit that accidentally
        # writes a scalar.  Without this filter, ``_infer_provider``
        # would try ``model.startswith(tuple(rules.get(...)))`` on a
        # string and produce a confusing TypeError at the first LLM call.
        _write_yaml(
            config_path,
            "default:\n"
            "  model_routing:\n"
            "    provider_inference:\n"
            "      anthropic_prefixes: claude\n"  # scalar, not list
            "      openai_exact: [o1]\n",
        )
        assert provider_inference() == {"openai_exact": ["o1"]}

    def test_unparseable_yaml_returns_empty_dict(self, config_path: Path) -> None:
        # Malformed YAML → ``_load_config`` logs a warning and returns
        # ``{}`` → caller falls back to hardcoded defaults.
        _write_yaml(config_path, ":::not yaml:::\n")
        assert provider_inference() == {}


class TestProviderInferenceImmutability:
    def test_returned_dict_is_fresh_per_call(self, config_path: Path) -> None:
        # The accessor copies both the outer dict and each inner list so
        # callers can mutate the result without poisoning the
        # ``_load_config`` lru_cache.  Without this, an in-place
        # ``rules["anthropic_prefixes"].append(...)`` in one call site
        # would silently leak to every subsequent ``_infer_provider``
        # invocation in the process.
        _write_yaml(
            config_path,
            "default:\n"
            "  model_routing:\n"
            "    provider_inference:\n"
            "      anthropic_prefixes: [claude]\n",
        )
        a = provider_inference()
        a["anthropic_prefixes"].append("evil")
        a["new_key"] = ["ignored"]
        b = provider_inference()
        assert b == {"anthropic_prefixes": ["claude"]}


class TestResetCache:
    def test_reset_cache_picks_up_new_file(self, config_path: Path) -> None:
        _write_yaml(
            config_path,
            "default:\n"
            "  model_routing:\n"
            "    provider_inference:\n"
            "      anthropic_prefixes: [claude]\n",
        )
        first = provider_inference()
        assert first == {"anthropic_prefixes": ["claude"]}

        # Without ``reset_cache`` the second read returns the cached
        # parse from the first call, even after the file is rewritten.
        _write_yaml(
            config_path,
            "default:\n"
            "  model_routing:\n"
            "    provider_inference:\n"
            "      anthropic_prefixes: [claude, anthropic-]\n",
        )
        cached = provider_inference()
        assert cached == first  # still the cached value

        reset_cache()
        fresh = provider_inference()
        assert fresh == {"anthropic_prefixes": ["claude", "anthropic-"]}


class TestShippedYamlMatchesHardcodedDefaults:
    """Pin the invariant that the YAML-shipped values match the constants
    in :mod:`agents.llm_client` exactly.

    If they ever diverge, an operator deploying without
    ``config/optimization.yaml`` would get different routing than one
    deploying with it — which is precisely the surprise the
    ``provider_inference()`` accessor exists to prevent.  Catching the
    drift here is cheaper than catching it as a misrouted request in
    production.
    """

    def test_shipped_yaml_matches_constants(self) -> None:
        # Force the loader off the test fixture path back to the
        # repo-default ``config/optimization.yaml``.
        import os

        from agents.llm_client import (
            _OPENAI_EXACT_MODELS,
            _OPENAI_PREFIX_MODELS,
        )

        os.environ.pop("PERSATRIX_OPTIMIZATION_CONFIG", None)
        reset_cache()
        try:
            shipped = provider_inference()
        finally:
            reset_cache()

        # Tolerate the file simply being absent in dev checkouts that
        # have not yet generated the YAML — the rest of the suite still
        # exercises the resolver paths.
        if not shipped:
            pytest.skip("config/optimization.yaml absent in this checkout")

        assert tuple(shipped.get("anthropic_prefixes", ())) == ("claude",)
        assert frozenset(shipped.get("openai_exact", ())) == _OPENAI_EXACT_MODELS
        assert tuple(shipped.get("openai_prefixes", ())) == _OPENAI_PREFIX_MODELS


def test_module_exports_provider_inference() -> None:
    """Pin the public surface so a refactor cannot silently drop the
    accessor from ``__all__`` — call sites in ``agents.llm_client``
    rely on the import path."""
    assert "provider_inference" in optimization.__all__


# ─── models.aliases accessor (RFC 0033 Phase 1 PR 1) ──────────


class TestModelAliasesAccessor:
    """``model_aliases()`` exposes the top-level ``models.aliases`` block.

    Unlike ``provider_inference()`` the block is NOT profile-scoped — it
    sits at the top level alongside ``default`` / ``cost`` (RFC 0033 §B),
    so resolution does not consult ``active_profile``.
    """

    def test_missing_file_returns_empty_dict(self, config_path: Path) -> None:
        assert model_aliases() == {}

    def test_no_models_block_returns_empty_dict(self, config_path: Path) -> None:
        _write_yaml(config_path, "active_profile: default\n")
        assert model_aliases() == {}

    def test_models_without_aliases_returns_empty_dict(
        self, config_path: Path,
    ) -> None:
        _write_yaml(config_path, "models:\n  something_else: {}\n")
        assert model_aliases() == {}

    def test_aliases_block_parses(self, config_path: Path) -> None:
        _write_yaml(
            config_path,
            'schema_version: "0.2"\n'
            "models:\n"
            "  aliases:\n"
            "    quality:\n"
            "      provider: anthropic\n"
            "      model: claude-sonnet-4-6\n"
            "      input_per_1m_tokens: 3.00\n"
            "      output_per_1m_tokens: 15.00\n",
        )
        assert model_aliases() == {
            "quality": {
                "provider": "anthropic",
                "model": "claude-sonnet-4-6",
                "input_per_1m_tokens": 3.00,
                "output_per_1m_tokens": 15.00,
            },
        }

    def test_openai_alias_round_trips(self, config_path: Path) -> None:
        _write_yaml(
            config_path,
            "models:\n"
            "  aliases:\n"
            "    quality-openai:\n"
            "      provider: openai\n"
            "      model: gpt-4o\n"
            "      input_per_1m_tokens: 2.50\n"
            "      output_per_1m_tokens: 10.00\n"
            "      provider_config:\n"
            "        base_url: https://api.openai.com/v1\n",
        )
        result = model_aliases()
        assert result["quality-openai"]["provider"] == "openai"
        assert result["quality-openai"]["provider_config"] == {
            "base_url": "https://api.openai.com/v1",
        }

    def test_non_dict_entries_are_filtered(self, config_path: Path) -> None:
        # A scalar where an alias entry should be is dropped rather than
        # surfaced as a malformed record the resolver would choke on.
        _write_yaml(
            config_path,
            "models:\n"
            "  aliases:\n"
            "    quality:\n"
            "      provider: anthropic\n"
            "      model: claude-sonnet-4-6\n"
            "      input_per_1m_tokens: 3.0\n"
            "      output_per_1m_tokens: 15.0\n"
            "    bogus: not-a-mapping\n",
        )
        assert set(model_aliases()) == {"quality"}

    def test_returned_map_is_fresh_per_call(self, config_path: Path) -> None:
        # The accessor copies the outer dict and each inner entry so a
        # caller mutating the result cannot poison the lru_cache.
        _write_yaml(
            config_path,
            "models:\n"
            "  aliases:\n"
            "    quality:\n"
            "      provider: anthropic\n"
            "      model: claude-sonnet-4-6\n"
            "      input_per_1m_tokens: 3.0\n"
            "      output_per_1m_tokens: 15.0\n",
        )
        a = model_aliases()
        a["quality"]["model"] = "tampered"
        a["injected"] = {}
        b = model_aliases()
        assert b == {
            "quality": {
                "provider": "anthropic",
                "model": "claude-sonnet-4-6",
                "input_per_1m_tokens": 3.0,
                "output_per_1m_tokens": 15.0,
            },
        }


class TestShippedYamlModelAliases:
    """Pin the alias blocks shipped in ``config/optimization.yaml`` and the
    per-provider demo configs under ``config/demo/``.

    The on-disk files are the single source of truth, so a drift between the
    promised aliases and the shipped blocks surfaces here. v0.3.4 "no default
    provider": the base config ships role aliases UNCONFIGURED; the concrete
    provider/model mappings live in the demo configs.
    """

    @staticmethod
    def _aliases_for(config_path: str | None) -> dict:
        import os

        prior = os.environ.get("PERSATRIX_OPTIMIZATION_CONFIG")
        if config_path is None:
            os.environ.pop("PERSATRIX_OPTIMIZATION_CONFIG", None)
        else:
            os.environ["PERSATRIX_OPTIMIZATION_CONFIG"] = config_path
        reset_cache()
        try:
            return model_aliases()
        finally:
            if prior is not None:
                os.environ["PERSATRIX_OPTIMIZATION_CONFIG"] = prior
            else:
                os.environ.pop("PERSATRIX_OPTIMIZATION_CONFIG", None)
            reset_cache()

    def test_base_config_ships_unconfigured_role_aliases(self) -> None:
        aliases = self._aliases_for(None)
        if not aliases:
            pytest.skip("config/optimization.yaml absent in this checkout")
        # No default provider: role aliases ship as the `unconfigured` sentinel.
        for role in ("quality", "fast", "summarizer"):
            assert aliases[role]["provider"] == "unconfigured", role

    def test_anthropic_demo_carries_core_aliases(self) -> None:
        aliases = self._aliases_for("config/demo/anthropic/optimization.yaml")
        if not aliases:
            pytest.skip("config/demo/anthropic absent in this checkout")
        # quality → Sonnet 4.6; fast / summarizer → Haiku.
        assert aliases["quality"]["provider"] == "anthropic"
        assert aliases["quality"]["model"] == "claude-sonnet-4-6"
        for alias in ("fast", "summarizer"):
            assert aliases[alias]["provider"] == "anthropic"
            assert aliases[alias]["model"] == "claude-haiku-4-5-20251001"

    def test_openai_demo_ships_priced_peer(self) -> None:
        aliases = self._aliases_for("config/demo/openai/optimization.yaml")
        if not aliases:
            pytest.skip("config/demo/openai absent in this checkout")
        openai_aliases = [
            name for name, entry in aliases.items()
            if entry.get("provider") == "openai"
        ]
        assert openai_aliases, "expected at least one priced OpenAI alias"
        for name in openai_aliases:
            assert float(aliases[name]["input_per_1m_tokens"]) > 0
            assert float(aliases[name]["output_per_1m_tokens"]) > 0


def test_module_exports_model_aliases() -> None:
    """Pin ``model_aliases`` on the public surface — the resolver in
    ``agents.model_aliases`` imports it."""
    assert "model_aliases" in optimization.__all__
