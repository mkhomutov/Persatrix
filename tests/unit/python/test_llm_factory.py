"""Tests for the provider factory (``agents.llm_factory``).

PR 2 of the RFC 0033 PR plan rewires ``create_provider`` to consume the
alias resolver and return ``(provider, physical_model)``. These tests pin
the §D precedence rules and the raw-ID deprecation signal (the one-shot
warning + the per-agent ``persatrix.llm.alias.raw_id_usage`` counter that
gates Phase 3).

``create_provider`` is exercised through its public
``agents.llm_client`` re-export to also pin that import path; the
process-wide raw-ID dedup state and the ``try_get_instruments`` hook live
in ``agents.llm_factory``, so those patch/monkeypatch targets point there.
All tests use mocked SDK modules — no real API calls.
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

from agents import llm_factory, optimization
from agents.llm_client import AnthropicProvider, OpenAIProvider, create_provider
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


@pytest.fixture
def _reset_raw_id_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset the process-wide raw-ID dedup state so each test starts clean."""
    monkeypatch.setattr(llm_factory, "_raw_id_warning_emitted", False)
    monkeypatch.setattr(llm_factory, "_raw_id_counted_agents", set())


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


class TestCreateProviderRawIdSignal:
    """RFC 0033 PR 2 — raw vendor IDs warn once + feed the Phase 3 gate counter."""

    def test_raw_id_emits_deprecation_warning_once_per_process(
        self, _reset_raw_id_signal: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        with use_alias_map(_ALIAS_MAP), patch.dict(
            sys.modules, {"anthropic": _mock_anthropic_module()}
        ), caplog.at_level(logging.WARNING, logger="agents.llm_factory"):
            _, model = create_provider(
                {"id": "raw-agent", "model": "claude-sonnet-4-20250514"}
            )
            # Second creation for the same raw agent must not re-warn.
            create_provider({"id": "raw-agent", "model": "claude-sonnet-4-20250514"})
        deprecations = [r for r in caplog.records if "RFC 0033" in r.getMessage()]
        assert len(deprecations) == 1
        assert "claude-sonnet-4-20250514" in deprecations[0].getMessage()
        # The physical id still flows through unchanged on the raw path.
        assert model == "claude-sonnet-4-20250514"

    def test_raw_id_increments_counter_once_per_agent(
        self, _reset_raw_id_signal: None
    ) -> None:
        fake_inst = MagicMock()
        with use_alias_map(_ALIAS_MAP), patch.dict(
            sys.modules, {"anthropic": _mock_anthropic_module()}
        ), patch("agents.llm_factory.try_get_instruments", return_value=fake_inst):
            create_provider({"id": "raw-x", "model": "claude-sonnet-4-20250514"})
            create_provider({"id": "raw-x", "model": "claude-sonnet-4-20250514"})
        fake_inst.alias_raw_id_usage.add.assert_called_once_with(
            1, attributes={"agent.id": "raw-x"}
        )

    def test_raw_id_counter_retries_when_instruments_unavailable_first(
        self, _reset_raw_id_signal: None
    ) -> None:
        """The Phase 3 gate counter must not silently under-read.

        The counter de-dup records an agent only *after* a successful emit:
        if the first ``create_provider`` runs before metrics init
        (``try_get_instruments()`` returns ``None``), a later call once
        instruments exist must still count the agent — otherwise the gate
        counter under-reads and could falsely open. ``side_effect=[None,
        fake_inst]`` simulates instruments coming up between the two calls.
        """
        fake_inst = MagicMock()
        with use_alias_map(_ALIAS_MAP), patch.dict(
            sys.modules, {"anthropic": _mock_anthropic_module()}
        ), patch(
            "agents.llm_factory.try_get_instruments",
            side_effect=[None, fake_inst],
        ):
            create_provider({"id": "raw-late", "model": "claude-sonnet-4-20250514"})
            create_provider({"id": "raw-late", "model": "claude-sonnet-4-20250514"})
        fake_inst.alias_raw_id_usage.add.assert_called_once_with(
            1, attributes={"agent.id": "raw-late"}
        )

    def test_alias_path_emits_no_raw_id_signal(
        self, _reset_raw_id_signal: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        fake_inst = MagicMock()
        with use_alias_map(_ALIAS_MAP), patch.dict(
            sys.modules, {"anthropic": _mock_anthropic_module()}
        ), patch(
            "agents.llm_factory.try_get_instruments", return_value=fake_inst
        ), caplog.at_level(logging.WARNING, logger="agents.llm_factory"):
            create_provider({"id": "q", "model": "quality"})
        assert not any("RFC 0033" in r.getMessage() for r in caplog.records)
        fake_inst.alias_raw_id_usage.add.assert_not_called()


class TestStockConfigMigration:
    """RFC 0033 PR 3 — after the config migration, a clean checkout starts
    its default agents through the ``quality`` alias (physical model
    ``claude-sonnet-4-6``), so no stock agent carries a raw vendor ID and
    ``create_provider`` fires no RFC 0033 deprecation warning for them.

    These tests read the *shipped* ``config/optimization.yaml`` (the alias
    map) and ``config/agents.yaml`` (the migrated entries), so they fail
    until both migrations land.
    """

    @staticmethod
    def _shipped_agents() -> list[dict[str, Any]]:
        repo_root = Path(__file__).resolve().parents[3]
        with (repo_root / "config" / "agents.yaml").open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return list(data["agents"])

    def test_every_stock_agent_resolves_to_quality_physical_model(self) -> None:
        os.environ.pop("PERSATRIX_OPTIMIZATION_CONFIG", None)
        optimization.reset_cache()
        try:
            for agent in self._shipped_agents():
                resolved = resolve(agent["model"])
                assert resolved.raw is False, f"{agent['id']} still uses a raw vendor ID"
                assert resolved.alias == "quality", agent["id"]
                assert resolved.provider == "anthropic", agent["id"]
                assert resolved.model == "claude-sonnet-4-6", agent["id"]
        finally:
            optimization.reset_cache()

    def test_create_provider_for_stock_agents_emits_no_raw_id_warning(
        self, _reset_raw_id_signal: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        os.environ.pop("PERSATRIX_OPTIMIZATION_CONFIG", None)
        optimization.reset_cache()
        try:
            agents = self._shipped_agents()
            with patch.dict(
                sys.modules, {"anthropic": _mock_anthropic_module()}
            ), caplog.at_level(logging.WARNING, logger="agents.llm_factory"):
                for agent in agents:
                    create_provider(agent)
            assert not any(
                "RFC 0033" in r.getMessage() for r in caplog.records
            ), "a stock agent still trips the raw-ID deprecation warning"
        finally:
            optimization.reset_cache()
