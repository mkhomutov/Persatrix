"""Unit tests for ``resolve_watsonx_config`` (``agents.llm_watsonx``).

The pure resolver mirrors ``resolve_ollama_base_url``: per field, the alias/agent
``provider_config`` wins, then the ``WATSONX_*`` env, then (``url`` only) the
us-south default. These are NON-secret config — the env channel keeps the tracked
demo config generic and an operator's project id out of VCS (RFC 0053 §C, PR 2
amendment). Split from ``test_llm_watsonx.py`` (translation logic) so neither
file crosses the 500-line review cap — the ``test_llm_factory_watsonx.py``
precedent. The ``create_provider`` routing that consumes this resolver
(env-fallback + fail-closed) lives in ``test_llm_factory_watsonx.py``.
"""

from __future__ import annotations

import pytest

from agents.llm_watsonx import DEFAULT_WATSONX_URL, resolve_watsonx_config


@pytest.fixture(autouse=True)
def _clear_watsonx_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hermetic resolver tests: no leakage from a developer shell / .env."""
    for var in ("WATSONX_URL", "WATSONX_PROJECT_ID", "WATSONX_SPACE_ID"):
        monkeypatch.delenv(var, raising=False)


def test_resolve_defaults_url_when_unset() -> None:
    """No config and no env → us-south default url, no id."""
    assert resolve_watsonx_config() == (DEFAULT_WATSONX_URL, None, None)
    assert resolve_watsonx_config(None) == (DEFAULT_WATSONX_URL, None, None)
    assert resolve_watsonx_config({}) == (DEFAULT_WATSONX_URL, None, None)


def test_resolve_reads_provider_config() -> None:
    url, project_id, space_id = resolve_watsonx_config(
        {"project_id": "p1", "url": "https://eu-de.ml.cloud.ibm.com"}
    )
    assert url == "https://eu-de.ml.cloud.ibm.com"
    assert project_id == "p1"
    assert space_id is None


def test_resolve_empty_config_value_falls_through_to_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A demo config that ships ``project_id: ""``/``url: ""`` must fall through
    to the env, not be taken as a (blank) answer."""
    monkeypatch.setenv("WATSONX_PROJECT_ID", "env-p")
    monkeypatch.setenv("WATSONX_URL", "https://jp-tok.ml.cloud.ibm.com")
    url, project_id, _space = resolve_watsonx_config({"project_id": "", "url": ""})
    assert project_id == "env-p"
    assert url == "https://jp-tok.ml.cloud.ibm.com"


def test_resolve_env_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WATSONX_PROJECT_ID", "env-p")
    monkeypatch.setenv("WATSONX_SPACE_ID", "env-s")
    monkeypatch.setenv("WATSONX_URL", "https://jp-tok.ml.cloud.ibm.com")
    url, project_id, space_id = resolve_watsonx_config()
    assert url == "https://jp-tok.ml.cloud.ibm.com"
    assert project_id == "env-p"
    assert space_id == "env-s"


def test_resolve_provider_config_wins_over_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WATSONX_PROJECT_ID", "env-p")
    monkeypatch.setenv("WATSONX_URL", "https://env.ml.cloud.ibm.com")
    url, project_id, _space = resolve_watsonx_config(
        {"project_id": "cfg-p", "url": "https://cfg.ml.cloud.ibm.com"}
    )
    assert project_id == "cfg-p"
    assert url == "https://cfg.ml.cloud.ibm.com"


def test_resolve_strips_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WATSONX_PROJECT_ID", "  env-p  ")
    _url, project_id, _space = resolve_watsonx_config({"project_id": "   "})
    # config value is whitespace-only → unset → env, itself stripped.
    assert project_id == "env-p"
