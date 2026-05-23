"""Tests for the local-model Ollama provider (agents.llm_ollama).

Real inference, but local: no API key and no cloud spend. These tests assert
that the env knobs resolve correctly, that OllamaProvider is a thin
OpenAI-compatible subclass (sentinel key, localhost default,
``gen_ai.system`` name ``ollama``, forced-model substitution), and that
``create_provider`` routes to it under ``PERSATRIX_OLLAMA`` / ``provider:
ollama`` — with offline mode winning if both flags are set.

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
from agents.llm_offline import MockProvider
from agents.llm_ollama import (
    DEFAULT_OLLAMA_BASE_URL,
    DEFAULT_OLLAMA_MODEL,
    OllamaProvider,
    ollama_mode_enabled,
    resolve_ollama_base_url,
    resolve_ollama_model,
)
from agents.llm_types import StopReason


@pytest.fixture(autouse=True)
def _ollama_env_baseline(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Clear every Ollama/offline env knob so none leaks across tests."""
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


# ─── ollama_mode_enabled ────────────────────────────────────


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_ollama_mode_enabled_truthy(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("PERSATRIX_OLLAMA", value)
    assert ollama_mode_enabled() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "nope"])
def test_ollama_mode_enabled_falsy(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("PERSATRIX_OLLAMA", value)
    assert ollama_mode_enabled() is False


# ─── model / base-url resolution ────────────────────────────


def test_resolve_model_default() -> None:
    assert resolve_ollama_model() == DEFAULT_OLLAMA_MODEL


def test_resolve_model_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSATRIX_OLLAMA_MODEL", "qwen2.5:0.5b")
    assert resolve_ollama_model() == "qwen2.5:0.5b"


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


async def test_create_message_passes_model_verbatim_without_force() -> None:
    """Per-agent path (no force_model): the configured Ollama tag is used."""
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


async def test_create_message_force_model_overrides_caller_model() -> None:
    """Forced global mode substitutes the one pulled model for the caller's.

    A cloud model id (or the summariser's optimization.yaml model) passed in
    must still hit the single locally-pulled model.
    """
    mod, client = _mock_openai_module()
    client.chat.completions.create = AsyncMock(return_value=_openai_response())
    with patch.dict(sys.modules, {"openai": mod}):
        p = OllamaProvider(force_model="llama3.2")
    await p.create_message(
        model="claude-sonnet-4-6",
        messages=[],
        system="",
        tools=[],
        max_tokens=64,
        temperature=0.2,
    )
    assert client.chat.completions.create.call_args[1]["model"] == "llama3.2"


# ─── create_provider routing ────────────────────────────────


def test_create_provider_ollama_env_forces_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PERSATRIX_OLLAMA", "1")
    monkeypatch.setenv("PERSATRIX_OLLAMA_MODEL", "qwen2.5")
    mod, _client = _mock_openai_module()
    with patch.dict(sys.modules, {"openai": mod}):
        # A real cloud model id — forced Ollama mode must override it anyway.
        provider = create_provider({"id": "ember-owl", "model": "claude-sonnet-4-6"})
    assert isinstance(provider, OllamaProvider)
    assert provider.name == "ollama"
    # Forced mode threads the env model through as the substitution target.
    assert provider._force_model == "qwen2.5"


def test_create_provider_forced_mode_uses_base_url_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forced mode reaches the daemon at PERSATRIX_OLLAMA_BASE_URL.

    The compose overlay sets this env to the bridge endpoint, so the forced
    path must thread it into the constructed client (not the localhost default).
    """
    monkeypatch.setenv("PERSATRIX_OLLAMA", "1")
    monkeypatch.setenv("PERSATRIX_OLLAMA_BASE_URL", "http://ollama:11434/v1")
    mod, _client = _mock_openai_module()
    with patch.dict(sys.modules, {"openai": mod}):
        create_provider({"id": "ember-owl", "model": "claude-sonnet-4-6"})
    mod.AsyncOpenAI.assert_called_once_with(
        api_key="ollama", base_url="http://ollama:11434/v1"
    )


def test_create_provider_forced_mode_provider_config_still_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even forced, a per-agent provider_config.base_url beats the env.

    Pins the documented precedence (resolve_ollama_base_url): the per-agent
    override is the most specific source unconditionally, so an agent carrying
    a stray base_url is routed to it rather than the forced deployment endpoint.
    """
    monkeypatch.setenv("PERSATRIX_OLLAMA", "1")
    monkeypatch.setenv("PERSATRIX_OLLAMA_BASE_URL", "http://ollama:11434/v1")
    mod, _client = _mock_openai_module()
    with patch.dict(sys.modules, {"openai": mod}):
        create_provider(
            {
                "id": "x",
                "model": "claude-sonnet-4-6",
                "provider_config": {"base_url": "http://agent-host:11434/v1"},
            }
        )
    mod.AsyncOpenAI.assert_called_once_with(
        api_key="ollama", base_url="http://agent-host:11434/v1"
    )


def test_create_provider_explicit_ollama_without_env() -> None:
    mod, _client = _mock_openai_module()
    with patch.dict(sys.modules, {"openai": mod}):
        provider = create_provider(
            {"id": "x", "model": "llama3.2", "provider": "ollama"}
        )
    assert isinstance(provider, OllamaProvider)
    # Per-agent opt-in: the configured model is used verbatim, no force.
    assert provider._force_model is None


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


def test_offline_wins_when_both_flags_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """Offline mode is checked first — it needs no network or daemon."""
    monkeypatch.setenv("PERSATRIX_OFFLINE", "1")
    monkeypatch.setenv("PERSATRIX_OLLAMA", "1")
    provider = create_provider({"id": "x", "model": "claude-sonnet-4-6"})
    assert isinstance(provider, MockProvider)


def test_ollama_provider_reexported_from_llm_client() -> None:
    assert OllamaProviderReexport is OllamaProvider
