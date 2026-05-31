"""Tests for the ``agents.optimization`` config accessors.

Covers the model-routing accessors (``model_routing_defaults()`` /
``model_aliases()``) and cache invalidation via :func:`reset_cache`, so a
regression in the resolution chain surfaces here rather than as a runtime
model-routing mistake.

The legacy ``provider_inference()`` accessor — and its consumer
``agents.llm_client._infer_provider`` — were retired in RFC 0033 Phase 3
(deliverable 2): provider is declared on the alias map, never inferred
from a model-name prefix (RFC 0033 §H / §I). Its absence is pinned by
:func:`test_module_does_not_export_provider_inference`.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from agents import optimization
from agents.optimization import model_aliases, model_routing_defaults, reset_cache


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


class TestResetCache:
    def test_reset_cache_picks_up_new_file(self, config_path: Path) -> None:
        _write_yaml(
            config_path,
            "default:\n"
            "  model_routing:\n"
            "    defaults:\n"
            "      task_agents: quality\n",
        )
        first = model_routing_defaults()
        assert first == {"task_agents": "quality"}

        # Without ``reset_cache`` the second read returns the cached
        # parse from the first call, even after the file is rewritten.
        _write_yaml(
            config_path,
            "default:\n"
            "  model_routing:\n"
            "    defaults:\n"
            "      task_agents: fast\n",
        )
        cached = model_routing_defaults()
        assert cached == first  # still the cached value

        reset_cache()
        fresh = model_routing_defaults()
        assert fresh == {"task_agents": "fast"}


def test_module_does_not_export_provider_inference() -> None:
    """RFC 0033 Phase 3 (deliverable 2) — the ``provider_inference()``
    accessor is retired.

    It existed only to feed ``agents.llm_client._infer_provider`` (the
    raw-ID prefix-routing heuristic); with that heuristic deleted it has no
    consumer. Pin both its absence from ``__all__`` and from the module
    namespace so a refactor cannot silently resurrect the inference path
    the alias map (RFC 0033 §H) replaced.
    """
    assert "provider_inference" not in optimization.__all__
    assert not hasattr(optimization, "provider_inference")


# ─── models.aliases accessor (RFC 0033 Phase 1 PR 1) ──────────


class TestModelAliasesAccessor:
    """``model_aliases()`` exposes the top-level ``models.aliases`` block.

    Unlike the profile-scoped ``model_routing_defaults()`` accessor, the
    block is NOT profile-scoped — it sits at the top level alongside
    ``default`` / ``cost`` (RFC 0033 §B), so resolution does not consult
    ``active_profile``.
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
            'schema_version: "0.3"\n'
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


# ─── Shipped optimization.yaml — RFC 0033 Phase 3 (deliverable 3) ──


class TestShippedConfigPhase3:
    """Pin the on-disk ``config/optimization.yaml`` contract after the
    RFC 0033 Phase 3 raw-ID rejection cutover (deliverable 3):

    * ``schema_version`` is bumped to ``"0.3"`` — the marker the resolver
      no longer accepts raw vendor IDs (``models.aliases`` is the single
      source of truth; ``agents.model_aliases.resolve`` rejects anything
      else with a loud ``SystemExit``).
    * the ``default.model_routing.provider_inference`` block is gone — its
      only consumer was the deleted ``_infer_provider`` heuristic.
    """

    @staticmethod
    def _shipped() -> dict | None:
        import yaml

        path = Path("config/optimization.yaml")
        if not path.exists():
            return None
        return yaml.safe_load(path.read_text(encoding="utf-8"))

    def test_schema_version_is_0_3(self) -> None:
        cfg = self._shipped()
        if cfg is None:
            pytest.skip("config/optimization.yaml absent in this checkout")
        assert cfg.get("schema_version") == "0.3", (
            "RFC 0033 Phase 3 bumps the optimization schema_version to "
            f"'0.3' on raw-ID rejection (got {cfg.get('schema_version')!r})"
        )

    def test_provider_inference_block_is_removed(self) -> None:
        cfg = self._shipped()
        if cfg is None:
            pytest.skip("config/optimization.yaml absent in this checkout")
        routing = cfg.get("default", {}).get("model_routing", {})
        assert "provider_inference" not in routing, (
            "the provider_inference block is retired with _infer_provider "
            "(RFC 0033 §I / Phase 3) — provider is declared on the alias"
        )
