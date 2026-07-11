"""Tests for the provider factory (``agents.llm_factory``).

``create_provider`` consumes the alias resolver and returns
``(provider, physical_model)``. These tests pin the §D precedence rules.
As of RFC 0033 **Phase 3** the §E raw-vendor-ID pass-through is retired:
a ``model:`` that is not a declared alias is a loud ``SystemExit`` at
factory time, so there is no longer a deprecation warning or a
``persatrix.llm.alias.raw_id_usage`` gate counter to exercise here.

``create_provider`` is exercised through its public ``agents.llm_client``
re-export to also pin that import path. All tests use mocked SDK modules —
no real API calls.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from agents import optimization
from agents.llm_client import (
    AnthropicProvider,
    GeminiProvider,
    OpenAIProvider,
    create_provider,
)
from agents.model_aliases import resolve, use_alias_map

# A seeded alias map exercised through the resolver's ``use_alias_map`` test
# seam, so these tests never depend on the shipped config/optimization.yaml.
_ALIAS_MAP = {
    "quality": {
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "input_per_1m_tokens": 3.0,
        "output_per_1m_tokens": 15.0,
    },
    "quality-openai": {
        "provider": "openai",
        "model": "gpt-4o",
        "input_per_1m_tokens": 2.5,
        "output_per_1m_tokens": 10.0,
        "provider_config": {"base_url": "https://alias-host/v1"},
    },
}


def _mock_anthropic_module() -> MagicMock:
    mod = MagicMock()
    mod.AsyncAnthropic.return_value = AsyncMock()
    return mod


def _mock_openai_module() -> MagicMock:
    mod = MagicMock()
    mod.AsyncOpenAI.return_value = AsyncMock()
    return mod


def _mock_genai_modules() -> tuple[MagicMock, MagicMock]:
    """Return stand-in ``google`` / ``google.genai`` modules for GeminiProvider.

    ``from google import genai`` resolves ``genai`` as an attribute of the
    ``google`` package, so the fake ``google`` module carries a ``genai``
    attribute whose ``Client`` returns a client double.
    """
    genai_mod = MagicMock()
    genai_mod.Client.return_value = MagicMock()
    google_mod = MagicMock()
    google_mod.genai = genai_mod
    return google_mod, genai_mod


class TestCreateProviderAliasResolution:
    """RFC 0033 §D — create_provider resolves aliases to a physical model."""

    def test_alias_resolves_to_physical_anthropic_model(self) -> None:
        with use_alias_map(_ALIAS_MAP), patch.dict(
            sys.modules, {"anthropic": _mock_anthropic_module()}
        ):
            provider, model = create_provider({"id": "q", "model": "quality"})
        assert isinstance(provider, AnthropicProvider)
        # The physical vendor id reaches create_message — never the alias name.
        assert model == "claude-sonnet-4-6"

    def test_alias_provider_field_agreeing_is_accepted(self) -> None:
        """A redundant-but-agreeing provider: field is accepted (§D rule 1)."""
        with use_alias_map(_ALIAS_MAP), patch.dict(
            sys.modules, {"anthropic": _mock_anthropic_module()}
        ):
            provider, model = create_provider(
                {"id": "q", "model": "quality", "provider": "anthropic"}
            )
        assert isinstance(provider, AnthropicProvider)
        assert model == "claude-sonnet-4-6"

    def test_alias_provider_conflict_raises(self) -> None:
        """A disagreeing provider: field is a SystemExit naming both (§D rule 1)."""
        with use_alias_map(_ALIAS_MAP):
            with pytest.raises(SystemExit) as exc:
                create_provider(
                    {"id": "bad", "model": "quality", "provider": "openai"}
                )
        msg = str(exc.value)
        assert "quality" in msg
        assert "anthropic" in msg and "openai" in msg
        assert "bad" in msg

    def test_alias_provider_config_wins_over_agent_entry(self) -> None:
        """Alias-level provider_config beats the agent entry per-field (§D rule 2)."""
        mod = _mock_openai_module()
        with use_alias_map(_ALIAS_MAP), patch.dict(sys.modules, {"openai": mod}):
            provider, model = create_provider(
                {
                    "id": "o",
                    "model": "quality-openai",
                    "provider_config": {"base_url": "https://agent-host/v1"},
                }
            )
        assert isinstance(provider, OpenAIProvider)
        assert model == "gpt-4o"
        assert mod.AsyncOpenAI.call_args.kwargs["base_url"] == "https://alias-host/v1"


class TestCreateProviderRejectsRawId:
    """RFC 0033 Phase 3 — the §E raw-vendor-ID pass-through is retired.

    A ``model:`` that is not a declared alias no longer routes with a
    deprecation warning; it is a loud ``SystemExit`` at factory time,
    naming the offending string so an operator declares it as an alias.
    """

    def test_raw_vendor_id_raises_systemexit(self) -> None:
        with use_alias_map(_ALIAS_MAP), pytest.raises(SystemExit) as exc:
            create_provider({"id": "raw-agent", "model": "claude-sonnet-4-20250514"})
        assert "claude-sonnet-4-20250514" in str(exc.value)

    def test_raw_vendor_id_raises_before_any_provider_built(self) -> None:
        # The resolver rejects the raw id before the factory ever reaches a
        # provider branch, so no SDK module import is needed for the failure.
        with use_alias_map(_ALIAS_MAP), pytest.raises(SystemExit):
            create_provider({"id": "raw-o", "model": "gpt-4o"})

    def test_alias_path_still_resolves(self) -> None:
        # The alias path is unaffected — a declared alias resolves normally.
        with use_alias_map(_ALIAS_MAP), patch.dict(
            sys.modules, {"anthropic": _mock_anthropic_module()}
        ):
            _, model = create_provider({"id": "q", "model": "quality"})
        assert model == "claude-sonnet-4-6"


class TestRoutingDefaultsAreReservedForTaskAgentsAndEvaluators:
    """ISSUE-0069 — ``defaults.task_agents`` / ``.evaluators`` are *reserved*.

    RFC 0033 §J wires only the ``sub_agents`` routing default (the
    ``SubAgentRequest`` ``None``-resolution). ``create_provider`` — the task-agent
    construction site — takes its model from the explicit ``model:`` field and
    **never** consults ``model_routing_defaults()``: a missing/empty ``model:``
    hard-stops rather than falling back to ``defaults.task_agents``. These tests
    pin that contract executably, so a future attempt to *wire* the fallback
    (ISSUE-0069 option 1) trips here instead of silently changing behaviour the
    config comment documents as reserved.

    A fully-resolvable routing default is present (patched), so the hard-stop is
    a deliberate no-fallback choice, not the mere absence of a target.
    """

    _ROUTING = {"task_agents": "quality", "sub_agents": "quality", "evaluators": "fast"}

    def test_empty_model_hard_stops_without_routing_fallback(self) -> None:
        with use_alias_map(_ALIAS_MAP), patch.object(
            optimization, "model_routing_defaults", return_value=self._ROUTING
        ):
            with pytest.raises(SystemExit) as exc:
                create_provider({"id": "t", "model": ""})
        # The empty-model hard-stop, not a route to defaults.task_agents.
        assert "empty" in str(exc.value)

    def test_absent_model_hard_stops_without_routing_fallback(self) -> None:
        with use_alias_map(_ALIAS_MAP), patch.object(
            optimization, "model_routing_defaults", return_value=self._ROUTING
        ):
            # An omitted model: key is a KeyError (schema-`required` rejects it
            # earlier via `make validate`), never a route to defaults.task_agents.
            with pytest.raises(KeyError) as exc:
                create_provider({"id": "t"})
        # The missing `model:` key itself, not an unrelated KeyError from a
        # deeper routing-fallback lookup.
        assert "model" in str(exc.value)


class TestStockConfigMigration:
    """RFC 0033 PR 3 + the v0.3.4 "no default provider" amendment.

    After the config migration every stock agent routes through a logical
    alias (``quality`` / ``fast`` / ``summarizer``), never a raw vendor ID. The
    shipped base ``config/optimization.yaml`` ships those aliases UNCONFIGURED
    (no default provider), so a stock agent fails loud against it until a
    provider is configured; against a *configured* artifact (the anthropic demo
    config) the same agents resolve to the physical model and fire no RFC 0033
    raw-ID deprecation warning. These read ``config/agents.yaml`` (the migrated
    entries) and the on-disk alias maps, so they fail until both land.
    """

    _ANTHROPIC_DEMO = "config/demo/anthropic/optimization.yaml"

    @staticmethod
    def _shipped_agents() -> list[dict[str, Any]]:
        repo_root = Path(__file__).resolve().parents[3]
        with (repo_root / "config" / "agents.yaml").open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return list(data["agents"])

    def test_stock_agents_reference_aliases_not_raw_ids(self) -> None:
        # Migration invariant, provider-independent: every stock agent's model
        # is a declared role alias, never a raw vendor ID. (The base map is
        # unconfigured, but the *names* are still declared.)
        os.environ.pop("PERSATRIX_OPTIMIZATION_CONFIG", None)
        optimization.reset_cache()
        try:
            alias_names = set(optimization.model_aliases())
            if not alias_names:
                pytest.skip("config/optimization.yaml absent in this checkout")
            for agent in self._shipped_agents():
                assert agent["model"] in alias_names, (
                    f"{agent['id']} references {agent['model']!r}, not a declared alias"
                )
        finally:
            optimization.reset_cache()

    def test_stock_agents_fail_loud_against_unconfigured_base(self) -> None:
        # v0.3.4 "no default provider": resolving a stock agent's alias against
        # the shipped (unconfigured) base config is a loud, actionable error —
        # `docker compose up` with no demo/config does not silently route.
        os.environ.pop("PERSATRIX_OPTIMIZATION_CONFIG", None)
        optimization.reset_cache()
        try:
            if not optimization.model_aliases():
                pytest.skip("config/optimization.yaml absent in this checkout")
            agent = self._shipped_agents()[0]
            with pytest.raises(SystemExit, match="not configured"):
                resolve(agent["model"])
        finally:
            optimization.reset_cache()

    def test_every_stock_agent_resolves_against_configured_anthropic_demo(self) -> None:
        # Against the configured anthropic demo, the same alias-routed agents
        # resolve to the physical model (claude-sonnet-4-6) with no raw IDs.
        os.environ["PERSATRIX_OPTIMIZATION_CONFIG"] = self._ANTHROPIC_DEMO
        optimization.reset_cache()
        try:
            for agent in self._shipped_agents():
                resolved = resolve(agent["model"])
                # Phase 3 retired the raw pass-through, so a successful resolve
                # is already proof the agent routes via a declared alias.
                assert resolved.alias in {"quality", "fast", "summarizer"}, agent["id"]
                assert resolved.provider == "anthropic", agent["id"]
        finally:
            os.environ.pop("PERSATRIX_OPTIMIZATION_CONFIG", None)
            optimization.reset_cache()

    def test_create_provider_for_stock_agents_builds_physical_model(self) -> None:
        # Against the configured anthropic demo, every stock agent builds a
        # provider and returns the physical vendor id (never an alias name) —
        # the alias path the Phase 3 resolver leaves as the only path.
        os.environ["PERSATRIX_OPTIMIZATION_CONFIG"] = self._ANTHROPIC_DEMO
        optimization.reset_cache()
        try:
            agents = self._shipped_agents()
            with patch.dict(sys.modules, {"anthropic": _mock_anthropic_module()}):
                for agent in agents:
                    _, model = create_provider(agent)
                    assert model == "claude-sonnet-4-6", agent["id"]
        finally:
            os.environ.pop("PERSATRIX_OPTIMIZATION_CONFIG", None)
            optimization.reset_cache()


class TestCreateProviderGemini:
    """RFC 0053 — the ``provider: gemini`` factory branch.

    The GeminiProvider translation logic is tested in
    ``test_llm_gemini.py``; these pin the ``create_provider`` routing: alias
    resolution, the GEMINI_API_KEY→GOOGLE_API_KEY key fallback, the S-09
    warn-not-crash on a missing key, the ImportError→SystemExit install hint on
    a missing SDK, and the §D disagreeing-provider conflict.
    """

    _ALIAS = {
        "quality-gemini": {
            "provider": "gemini",
            "model": "gemini-2.5-pro",
            "input_per_1m_tokens": 1.25,
            "output_per_1m_tokens": 10.00,
        },
    }

    @pytest.fixture(autouse=True)
    def _clear_gemini_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
            monkeypatch.delenv(var, raising=False)

    def test_alias_routes_to_gemini_provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "key-123")
        google_mod, genai_mod = _mock_genai_modules()
        with use_alias_map(self._ALIAS), patch.dict(
            sys.modules, {"google": google_mod, "google.genai": genai_mod}
        ):
            provider, model = create_provider({"id": "g", "model": "quality-gemini"})
        assert isinstance(provider, GeminiProvider)
        # The physical vendor id reaches create_message — never the alias name.
        assert model == "gemini-2.5-pro"

    def test_google_api_key_is_the_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GOOGLE_API_KEY is the documented fallback when GEMINI_API_KEY is unset."""
        monkeypatch.setenv("GOOGLE_API_KEY", "goog-key")
        google_mod, genai_mod = _mock_genai_modules()
        with use_alias_map(self._ALIAS), patch.dict(
            sys.modules, {"google": google_mod, "google.genai": genai_mod}
        ):
            provider, _model = create_provider({"id": "g", "model": "quality-gemini"})
            assert isinstance(provider, GeminiProvider)
            provider._get_client()  # force the lazy client build
        genai_mod.Client.assert_called_once_with(api_key="goog-key")

    def test_missing_key_warns_not_crashes(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A missing key WARNS at startup (S-09) and still returns a provider —
        it must not crash (the client is built lazily, failing on first request)."""
        google_mod, genai_mod = _mock_genai_modules()
        with use_alias_map(self._ALIAS), patch.dict(
            sys.modules, {"google": google_mod, "google.genai": genai_mod}
        ):
            with caplog.at_level(logging.WARNING):
                provider, _model = create_provider(
                    {"id": "g", "model": "quality-gemini"}
                )
        assert isinstance(provider, GeminiProvider)
        assert any(
            "GEMINI_API_KEY" in r.message or "GOOGLE_API_KEY" in r.message
            for r in caplog.records
        )

    def test_missing_sdk_is_systemexit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A missing google-genai SDK is a loud, actionable SystemExit at factory
        time (the shared ImportError→SystemExit install-hint pattern)."""
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        # ``google`` mapped to None makes ``from google import genai`` raise
        # ImportError deterministically, regardless of what is installed.
        with use_alias_map(self._ALIAS), patch.dict(sys.modules, {"google": None}):
            with pytest.raises(SystemExit) as exc:
                create_provider({"id": "g", "model": "quality-gemini"})
        assert "google-genai" in str(exc.value)

    def test_disagreeing_provider_field_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A disagreeing explicit provider: field is a SystemExit (§D rule 1)."""
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        with use_alias_map(self._ALIAS):
            with pytest.raises(SystemExit) as exc:
                create_provider(
                    {"id": "bad", "model": "quality-gemini", "provider": "openai"}
                )
        msg = str(exc.value)
        assert "gemini" in msg and "openai" in msg
