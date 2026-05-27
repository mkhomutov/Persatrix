"""Tests for the local-model Ollama provider (agents.llm_ollama).

Real inference, but local: no API key and no cloud spend. These tests assert
that the daemon base URL resolves correctly, that OllamaProvider is a thin
OpenAI-compatible subclass (sentinel key, localhost default, ``gen_ai.system``
name ``ollama``), and that ``create_provider`` routes to it the **same
standard way** every other provider is selected — through the resolved
``provider`` field (an alias declaring ``provider: ollama``, or a per-agent
``provider: ollama``). There is no global env force-knob: RFC 0033 made
provider selection purely config/alias-driven (the v0.3.4 provider-parity
refactor removed ``PERSATRIX_OLLAMA`` / the forced-model substitution).

``PERSATRIX_OLLAMA_MODEL`` survives as a small *configuration* override (which
model the ollama-routed agents use — analogous to an API key, not a selection
knob); ``PERSATRIX_OLLAMA_BASE_URL`` likewise targets the daemon endpoint.

No network is touched: the ``openai`` SDK is mocked via ``sys.modules`` the
same way :mod:`tests.unit.python.test_llm_client` does for OpenAIProvider.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.llm_client import OllamaProvider as OllamaProviderReexport
from agents.llm_client import create_provider
from agents.llm_ollama import (
    DEFAULT_OLLAMA_BASE_URL,
    OllamaProvider,
    resolve_ollama_base_url,
)
from agents.llm_types import StopReason
from agents.model_aliases import use_alias_map


@pytest.fixture(autouse=True)
def _ollama_env_baseline(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Clear every Ollama/offline env var so none leaks across tests."""
    for var in (
        "PERSATRIX_OLLAMA",
        "PERSATRIX_OLLAMA_MODEL",
        "PERSATRIX_OLLAMA_BASE_URL",
        "PERSATRIX_OFFLINE",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


# ─── helpers ────────────────────────────────────────────────


def _mock_openai_module() -> tuple[MagicMock, AsyncMock]:
    """Return a stand-in ``openai`` module + its AsyncOpenAI client double."""
    mod = MagicMock()
    client = AsyncMock()
    mod.AsyncOpenAI.return_value = client
    return mod, client


def _openai_response(
    content: str | None = "Hi from Llama",
    finish_reason: str = "stop",
    prompt_tokens: int = 12,
    completion_tokens: int = 7,
) -> SimpleNamespace:
    """Build a mock OpenAI-compatible chat-completions response."""
    message = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(message=message, finish_reason=finish_reason)
    usage = SimpleNamespace(
        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
    )
    return SimpleNamespace(choices=[choice], usage=usage)


# ─── base-url resolution ────────────────────────────────────


def test_resolve_base_url_default() -> None:
    assert resolve_ollama_base_url() == DEFAULT_OLLAMA_BASE_URL
    assert resolve_ollama_base_url(None) == DEFAULT_OLLAMA_BASE_URL
    assert resolve_ollama_base_url({}) == DEFAULT_OLLAMA_BASE_URL


def test_resolve_base_url_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSATRIX_OLLAMA_BASE_URL", "http://ollama:11434/v1")
    assert resolve_ollama_base_url() == "http://ollama:11434/v1"


def test_resolve_base_url_provider_config_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-agent provider_config is the most specific source — beats env."""
    monkeypatch.setenv("PERSATRIX_OLLAMA_BASE_URL", "http://env-host:11434/v1")
    assert (
        resolve_ollama_base_url({"base_url": "http://agent-host:11434/v1"})
        == "http://agent-host:11434/v1"
    )


# ─── OllamaProvider ─────────────────────────────────────────


def test_provider_name_is_ollama() -> None:
    """The OTel gen_ai.system attribute must read 'ollama', not 'openai'."""
    assert OllamaProvider.name == "ollama"


def test_provider_uses_sentinel_key_and_default_base_url() -> None:
    mod, _client = _mock_openai_module()
    with patch.dict(sys.modules, {"openai": mod}):
        OllamaProvider()
    mod.AsyncOpenAI.assert_called_once_with(
        api_key="ollama", base_url=DEFAULT_OLLAMA_BASE_URL
    )


def test_provider_explicit_base_url_and_key() -> None:
    mod, _client = _mock_openai_module()
    with patch.dict(sys.modules, {"openai": mod}):
        OllamaProvider(base_url="http://ollama:11434/v1", api_key="real")
    mod.AsyncOpenAI.assert_called_once_with(
        api_key="real", base_url="http://ollama:11434/v1"
    )


async def test_create_message_passes_model_verbatim() -> None:
    """The model the call site sends reaches the daemon unchanged.

    Provider selection no longer substitutes the model in-provider; the
    factory resolves the physical model and threads it through.
    """
    mod, client = _mock_openai_module()
    client.chat.completions.create = AsyncMock(return_value=_openai_response())
    with patch.dict(sys.modules, {"openai": mod}):
        p = OllamaProvider()  # __init__ adopts `client` as self._client
    resp = await p.create_message(
        model="llama3.2",
        messages=[{"role": "user", "content": "hi"}],
        system="",
        tools=[],
        max_tokens=64,
        temperature=0.2,
    )
    assert resp.text == "Hi from Llama"
    assert resp.stop_reason == StopReason.END_TURN
    assert resp.usage.input_tokens == 12
    assert resp.usage.output_tokens == 7
    assert client.chat.completions.create.call_args[1]["model"] == "llama3.2"


# ─── create_provider routing ────────────────────────────────


def test_create_provider_explicit_ollama_per_agent() -> None:
    """Per-agent ``provider: ollama`` routes to OllamaProvider; the
    configured Ollama tag is used verbatim."""
    mod, _client = _mock_openai_module()
    with patch.dict(sys.modules, {"openai": mod}):
        provider, model = create_provider(
            {"id": "x", "model": "llama3.2", "provider": "ollama"}
        )
    assert isinstance(provider, OllamaProvider)
    assert model == "llama3.2"


def test_create_provider_ollama_respects_provider_config_base_url() -> None:
    mod, _client = _mock_openai_module()
    with patch.dict(sys.modules, {"openai": mod}):
        create_provider(
            {
                "id": "x",
                "model": "llama3.2",
                "provider": "ollama",
                "provider_config": {"base_url": "http://gpu-box:11434/v1"},
            }
        )
    mod.AsyncOpenAI.assert_called_once_with(
        api_key="ollama", base_url="http://gpu-box:11434/v1"
    )


def test_create_provider_ollama_model_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``PERSATRIX_OLLAMA_MODEL`` overrides the model for ollama-routed agents.

    This is a configuration override (which local model to run — and it keeps
    the demo's ``ollama-pull`` and the agents in lock-step), *not* a provider-
    selection knob: it only takes effect once an agent is already routed to
    ollama via its alias/config. The overridden tag reaches the call site.
    """
    monkeypatch.setenv("PERSATRIX_OLLAMA_MODEL", "qwen2.5")
    mod, _client = _mock_openai_module()
    with patch.dict(sys.modules, {"openai": mod}):
        provider, model = create_provider(
            {"id": "x", "model": "llama3.2", "provider": "ollama"}
        )
    assert isinstance(provider, OllamaProvider)
    assert model == "qwen2.5"


def test_create_provider_ollama_env_does_not_force_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The removed ``PERSATRIX_OLLAMA`` knob no longer forces ollama.

    With the agent routed to a real cloud model and no ollama alias/override,
    the legacy env has no effect — the agent resolves to its configured
    provider.
    """
    monkeypatch.setenv("PERSATRIX_OLLAMA", "1")
    provider, _model = create_provider(
        {"id": "ember-owl", "model": "claude-sonnet-4-6"}
    )
    assert not isinstance(provider, OllamaProvider)
    assert provider.name == "anthropic"


def test_alias_declaring_ollama_resolves_through_same_branch() -> None:
    """An alias whose entry declares ``provider: ollama`` routes through the
    same standard provider branch as a per-agent ``provider: ollama``, with the
    alias's ``provider_config.base_url`` honoured (#423 stays green after the
    resolver lands). The alias's physical model reaches the call site, not the
    alias name.
    """
    alias_map = {
        "local": {
            "provider": "ollama",
            "model": "llama3.2",
            "input_per_1m_tokens": 0,
            "output_per_1m_tokens": 0,
            "provider_config": {"base_url": "http://gpu-box:11434/v1"},
        },
    }
    mod, _client = _mock_openai_module()
    with use_alias_map(alias_map), patch.dict(sys.modules, {"openai": mod}):
        provider, model = create_provider({"id": "x", "model": "local"})
    assert isinstance(provider, OllamaProvider)
    assert model == "llama3.2"
    mod.AsyncOpenAI.assert_called_once_with(
        api_key="ollama", base_url="http://gpu-box:11434/v1"
    )


def test_ollama_provider_reexported_from_llm_client() -> None:
    assert OllamaProviderReexport is OllamaProvider
