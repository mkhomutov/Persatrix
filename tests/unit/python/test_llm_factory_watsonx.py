"""Factory-branch tests for the ``provider: watsonx`` path (``create_provider``).

RFC 0053 PR 2. Split from ``test_llm_factory.py`` (which pins the shared §D
resolution rules + the other providers' branches) so neither file crosses the
500-line review cap — the ``test_llm_gemini_edge.py`` split precedent.

The WatsonxProvider translation logic (and the ``resolve_watsonx_config``
precedence/default rules) are tested in ``test_llm_watsonx.py``; these pin the
``create_provider`` routing. watsonx is the deliberate departure from the softer
missing-*key* warning: a ``project_id`` (or ``space_id``) is **required config
the client cannot be built without**, and — unlike a recoverable-per-request
missing key — the factory **fails closed at construction** (a loud SystemExit)
when it is absent. That id (and the regional ``url``) are **config, not
secrets**: their source of truth is the alias ``provider_config`` (the OpenAI
``base_url`` channel), but each also accepts a ``WATSONX_*`` env fallback
(``resolve_watsonx_config``) so the demo config can ship generic — only the
secret ``WATSONX_API_KEY`` is env-only. ``url`` carries a us-south default, so
only a missing id fails closed. All tests use mocked SDK modules — no real API
calls.
"""

from __future__ import annotations

import logging
import sys
from unittest.mock import patch

import pytest

from agents.llm_client import WatsonxProvider, create_provider
from agents.llm_watsonx import DEFAULT_WATSONX_URL
from agents.model_aliases import use_alias_map

from ._watsonx_test_helpers import _mock_watsonx_modules


class TestCreateProviderWatsonx:
    _URL = "https://us-south.ml.cloud.ibm.com"

    _ALIAS = {
        "quality-watsonx": {
            "provider": "watsonx",
            "model": "meta-llama/llama-3-3-70b-instruct",
            "input_per_1m_tokens": 1.80,
            "output_per_1m_tokens": 1.80,
            "provider_config": {"project_id": "proj-1", "url": _URL},
        },
        # No project_id/space_id — the client cannot construct without one.
        "no-project": {
            "provider": "watsonx",
            "model": "ibm/granite-3-8b-instruct",
            "input_per_1m_tokens": 0.20,
            "output_per_1m_tokens": 0.20,
            "provider_config": {"url": _URL},
        },
        # No url — the regional endpoint is required.
        "no-url": {
            "provider": "watsonx",
            "model": "ibm/granite-3-8b-instruct",
            "input_per_1m_tokens": 0.20,
            "output_per_1m_tokens": 0.20,
            "provider_config": {"project_id": "proj-1"},
        },
        # space_id is the documented alternative to project_id.
        "with-space": {
            "provider": "watsonx",
            "model": "ibm/granite-3-8b-instruct",
            "input_per_1m_tokens": 0.20,
            "output_per_1m_tokens": 0.20,
            "provider_config": {"space_id": "space-9", "url": _URL},
        },
    }

    @pytest.fixture(autouse=True)
    def _set_watsonx_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Present by default; individual tests delete it to exercise S-09.
        monkeypatch.setenv("WATSONX_API_KEY", "wx-key")
        # Hermetic: clear the NON-secret env fallbacks so a developer's shell (or
        # a populated .env) cannot make a fail-closed test silently pass by
        # resolving project_id/url from the environment. Tests that exercise the
        # env path set them explicitly.
        monkeypatch.delenv("WATSONX_PROJECT_ID", raising=False)
        monkeypatch.delenv("WATSONX_SPACE_ID", raising=False)
        monkeypatch.delenv("WATSONX_URL", raising=False)

    def test_alias_routes_to_watsonx_provider(self) -> None:
        ibm_mod, fm_mod, _model = _mock_watsonx_modules()
        with use_alias_map(self._ALIAS), patch.dict(
            sys.modules,
            {"ibm_watsonx_ai": ibm_mod, "ibm_watsonx_ai.foundation_models": fm_mod},
        ):
            provider, model = create_provider({"id": "w", "model": "quality-watsonx"})
        assert isinstance(provider, WatsonxProvider)
        # The physical vendor id reaches create_message — never the alias name.
        assert model == "meta-llama/llama-3-3-70b-instruct"

    def test_space_id_alternative_is_accepted(self) -> None:
        ibm_mod, fm_mod, _model = _mock_watsonx_modules()
        with use_alias_map(self._ALIAS), patch.dict(
            sys.modules,
            {"ibm_watsonx_ai": ibm_mod, "ibm_watsonx_ai.foundation_models": fm_mod},
        ):
            provider, _physical = create_provider({"id": "w", "model": "with-space"})
        assert isinstance(provider, WatsonxProvider)

    def test_missing_project_id_and_space_id_fails_closed(self) -> None:
        """Neither project_id nor space_id → a loud SystemExit at construction
        (required config, not the softer missing-key warning)."""
        with use_alias_map(self._ALIAS):
            with pytest.raises(SystemExit) as exc:
                create_provider({"id": "w", "model": "no-project"})
        msg = str(exc.value)
        assert "watsonx" in msg
        assert "project_id" in msg

    def test_missing_url_defaults_to_us_south(self) -> None:
        """No url anywhere → the us-south default, NOT a fail-closed. url has a
        sensible default (a wrong region is a loud request-time error), so it is
        no longer a construction-time blocker — only the id is."""
        ibm_mod, fm_mod, _model = _mock_watsonx_modules()
        with use_alias_map(self._ALIAS), patch.dict(
            sys.modules,
            {"ibm_watsonx_ai": ibm_mod, "ibm_watsonx_ai.foundation_models": fm_mod},
        ):
            provider, _physical = create_provider({"id": "w", "model": "no-url"})
        assert isinstance(provider, WatsonxProvider)
        assert provider._url == DEFAULT_WATSONX_URL

    def test_project_id_from_env_when_config_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The `no-project` alias (empty provider_config id) builds when the id is
        supplied via WATSONX_PROJECT_ID — the non-secret env fallback that lets
        the tracked demo config ship generic (RFC 0053 §C)."""
        monkeypatch.setenv("WATSONX_PROJECT_ID", "env-proj")
        ibm_mod, fm_mod, _model = _mock_watsonx_modules()
        with use_alias_map(self._ALIAS), patch.dict(
            sys.modules,
            {"ibm_watsonx_ai": ibm_mod, "ibm_watsonx_ai.foundation_models": fm_mod},
        ):
            provider, _physical = create_provider({"id": "w", "model": "no-project"})
        assert isinstance(provider, WatsonxProvider)
        assert provider._project_id == "env-proj"

    def test_space_id_from_env_is_accepted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """WATSONX_SPACE_ID is the env counterpart of the space_id alternative."""
        monkeypatch.setenv("WATSONX_SPACE_ID", "env-space")
        ibm_mod, fm_mod, _model = _mock_watsonx_modules()
        with use_alias_map(self._ALIAS), patch.dict(
            sys.modules,
            {"ibm_watsonx_ai": ibm_mod, "ibm_watsonx_ai.foundation_models": fm_mod},
        ):
            provider, _physical = create_provider({"id": "w", "model": "no-project"})
        assert isinstance(provider, WatsonxProvider)
        assert provider._space_id == "env-space"

    def test_provider_config_wins_over_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """provider_config is the source of truth: a set id there wins over the
        env fallback (the Ollama base_url precedence)."""
        monkeypatch.setenv("WATSONX_PROJECT_ID", "env-proj")
        monkeypatch.setenv("WATSONX_URL", "https://env.ml.cloud.ibm.com")
        ibm_mod, fm_mod, _model = _mock_watsonx_modules()
        with use_alias_map(self._ALIAS), patch.dict(
            sys.modules,
            {"ibm_watsonx_ai": ibm_mod, "ibm_watsonx_ai.foundation_models": fm_mod},
        ):
            # `quality-watsonx` sets project_id=proj-1 + url=_URL in provider_config.
            provider, _physical = create_provider({"id": "w", "model": "quality-watsonx"})
        assert isinstance(provider, WatsonxProvider)  # narrow off the LLMProvider protocol
        assert provider._project_id == "proj-1"
        assert provider._url == self._URL

    def test_url_from_env_overrides_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no url in provider_config, WATSONX_URL beats the us-south default —
        the knob that points the demo at another region (e.g. jp-tok) without
        editing the tracked config."""
        monkeypatch.setenv("WATSONX_URL", "https://jp-tok.ml.cloud.ibm.com")
        ibm_mod, fm_mod, _model = _mock_watsonx_modules()
        with use_alias_map(self._ALIAS), patch.dict(
            sys.modules,
            {"ibm_watsonx_ai": ibm_mod, "ibm_watsonx_ai.foundation_models": fm_mod},
        ):
            provider, _physical = create_provider({"id": "w", "model": "no-url"})
        assert isinstance(provider, WatsonxProvider)  # narrow off the LLMProvider protocol
        assert provider._url == "https://jp-tok.ml.cloud.ibm.com"

    def test_missing_config_fails_closed_before_sdk_import(self) -> None:
        """The required-config gate fires before the SDK is even imported — so a
        missing project_id is a config error, not a missing-SDK error, even when
        the SDK is absent."""
        with use_alias_map(self._ALIAS), patch.dict(
            sys.modules, {"ibm_watsonx_ai": None}
        ):
            with pytest.raises(SystemExit) as exc:
                create_provider({"id": "w", "model": "no-project"})
        # The config gate, not the ImportError→SystemExit install hint.
        assert "project_id" in str(exc.value)
        assert "ibm-watsonx-ai" not in str(exc.value)

    def test_missing_key_warns_not_crashes(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A missing *secret key* (config present) WARNS (S-09) and still returns
        a provider — the key is recoverable per-request, unlike required config."""
        monkeypatch.delenv("WATSONX_API_KEY", raising=False)
        ibm_mod, fm_mod, _model = _mock_watsonx_modules()
        with use_alias_map(self._ALIAS), patch.dict(
            sys.modules,
            {"ibm_watsonx_ai": ibm_mod, "ibm_watsonx_ai.foundation_models": fm_mod},
        ):
            with caplog.at_level(logging.WARNING):
                provider, _physical = create_provider({"id": "w", "model": "quality-watsonx"})
        assert isinstance(provider, WatsonxProvider)
        assert any("WATSONX_API_KEY" in r.message for r in caplog.records)

    def test_missing_sdk_is_systemexit(self) -> None:
        """A missing ibm-watsonx-ai SDK is a loud, actionable SystemExit at
        factory time (the shared ImportError→SystemExit install-hint pattern)."""
        # ``ibm_watsonx_ai`` mapped to None makes the provider's SDK import raise
        # ImportError deterministically, regardless of what is installed.
        with use_alias_map(self._ALIAS), patch.dict(
            sys.modules, {"ibm_watsonx_ai": None}
        ):
            with pytest.raises(SystemExit) as exc:
                create_provider({"id": "w", "model": "quality-watsonx"})
        assert "ibm-watsonx-ai" in str(exc.value)

    def test_disagreeing_provider_field_raises(self) -> None:
        """A disagreeing explicit provider: field is a SystemExit (§D rule 1)."""
        with use_alias_map(self._ALIAS):
            with pytest.raises(SystemExit) as exc:
                create_provider(
                    {"id": "bad", "model": "quality-watsonx", "provider": "openai"}
                )
        msg = str(exc.value)
        assert "watsonx" in msg and "openai" in msg
