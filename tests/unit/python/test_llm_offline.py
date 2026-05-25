"""Tests for the offline / mock LLM provider (agents.llm_offline).

No network, no API key, no cost — these tests assert exactly that the
MockProvider returns deterministic scripted or fallback text and that
create_provider routes to it under PERSATRIX_OFFLINE / provider: mock.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from agents.llm_client import MockProvider as MockProviderReexport
from agents.llm_client import create_provider
from agents.llm_offline import MockProvider, offline_mode_enabled, reset_cache
from agents.llm_types import StopReason

_FIXTURE = """\
responses:
  ember-owl:
    - match: ["flaky"]
      reply: "Run it 50 times on main."
    - match: ["q3", "blocking"]
      reply: "The auth migration is the long pole."
    - match: []
      reply: "Short version, please."
"""


@pytest.fixture(autouse=True)
def _offline_env_baseline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> Iterator[None]:
    """Deterministic baseline: env off, fixtures pointed at a temp file.

    Mirrors conftest's autouse env-isolation idiom so PERSATRIX_OFFLINE /
    PERSATRIX_OFFLINE_RESPONSES never leak across tests, and the cached
    fixture read is cleared before and after each test.
    """
    monkeypatch.delenv("PERSATRIX_OFFLINE", raising=False)
    fixture = tmp_path / "offline_responses.yaml"
    fixture.write_text(_FIXTURE, encoding="utf-8")
    monkeypatch.setenv("PERSATRIX_OFFLINE_RESPONSES", str(fixture))
    reset_cache()
    yield
    reset_cache()


# ─── offline_mode_enabled ───────────────────────────────────


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_offline_mode_enabled_truthy(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("PERSATRIX_OFFLINE", value)
    assert offline_mode_enabled() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "nope"])
def test_offline_mode_enabled_falsy(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("PERSATRIX_OFFLINE", value)
    assert offline_mode_enabled() is False


# ─── scripted replies ───────────────────────────────────────


async def test_scripted_reply_single_keyword() -> None:
    p = MockProvider(agent_id="ember-owl", display_name="Ember Owl")
    resp = await _call(p, "How would you triage a flaky integration test?")
    assert resp.text == "Run it 50 times on main."
    assert resp.stop_reason == StopReason.END_TURN
    assert resp.tool_calls == []


async def test_scripted_reply_requires_all_keywords() -> None:
    p = MockProvider(agent_id="ember-owl")
    # Only "blocking" present, not "q3" — that entry must NOT match; falls
    # through to the catch-all.
    resp = await _call(p, "what is blocking us?")
    assert resp.text == "Short version, please."
    # Both keywords present -> the specific entry wins.
    resp2 = await _call(p, "what's blocking the Q3 plan?")
    assert resp2.text == "The auth migration is the long pole."


async def test_scripted_reply_matches_block_content() -> None:
    """Latest-user-message extraction handles list-of-blocks content."""
    p = MockProvider(agent_id="ember-owl")
    resp = await p.create_message(
        model="mock",
        messages=[{"role": "user", "content": [{"type": "text", "text": "is it flaky?"}]}],
        system="",
        tools=[],
        max_tokens=100,
        temperature=0.7,
    )
    assert resp.text == "Run it 50 times on main."


async def test_scripted_reply_uses_latest_user_message() -> None:
    p = MockProvider(agent_id="ember-owl")
    resp = await p.create_message(
        model="mock",
        messages=[
            {"role": "user", "content": "an earlier flaky question"},
            {"role": "assistant", "content": "..."},
            {"role": "user", "content": "what's blocking the Q3 plan?"},
        ],
        system="",
        tools=[],
        max_tokens=100,
        temperature=0.7,
    )
    assert resp.text == "The auth migration is the long pole."


# ─── fallback ───────────────────────────────────────────────


async def test_fallback_when_no_fixture_for_agent() -> None:
    p = MockProvider(
        agent_id="not-in-fixture",
        display_name="Ghost",
        persona={"title": "Tester"},
    )
    resp = await _call(p, "anything at all")
    assert "offline demo mode" in resp.text
    assert "Ghost (Tester)" in resp.text
    assert resp.stop_reason == StopReason.END_TURN


async def test_fallback_is_deterministic() -> None:
    p = MockProvider(agent_id="ghost", display_name="Ghost")
    a = await _call(p, "same question")
    b = await _call(p, "same question")
    assert a.text == b.text


# ─── synthetic usage ────────────────────────────────────────


async def test_synthetic_usage_is_populated_but_no_real_call() -> None:
    p = MockProvider(agent_id="ember-owl")
    resp = await p.create_message(
        model="mock",
        messages=[{"role": "user", "content": "tell me about flaky tests"}],
        system="You are a helpful VP of Engineering.",
        tools=[],
        max_tokens=100,
        temperature=0.7,
    )
    assert resp.usage.input_tokens >= 1
    assert resp.usage.output_tokens >= 1


# ─── create_provider routing ────────────────────────────────


def test_create_provider_offline_env_forces_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSATRIX_OFFLINE", "1")
    # A real Anthropic model id — offline mode must override it anyway.
    provider, _model = create_provider(
        {"id": "ember-owl", "model": "claude-sonnet-4-20250514"}
    )
    assert isinstance(provider, MockProvider)
    assert provider.name == "mock"


def test_create_provider_explicit_mock_without_env() -> None:
    provider, _model = create_provider(
        {"id": "x", "model": "claude-sonnet-4-20250514", "provider": "mock"}
    )
    assert isinstance(provider, MockProvider)


def test_create_provider_offline_tolerates_placeholder_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Offline override is checked before the model field is read/validated."""
    monkeypatch.setenv("PERSATRIX_OFFLINE", "1")
    provider, _model = create_provider({"id": "x", "model": ""})
    assert isinstance(provider, MockProvider)


def test_create_provider_normal_path_unaffected() -> None:
    """With env off and no provider override, the real SDK provider is used."""
    provider, model = create_provider({"id": "x", "model": "claude-sonnet-4-20250514"})
    assert not isinstance(provider, MockProvider)
    assert provider.name == "anthropic"
    # The raw vendor id passes through unchanged (RFC 0033 §E).
    assert model == "claude-sonnet-4-20250514"


def test_mock_provider_reexported_from_llm_client() -> None:
    assert MockProviderReexport is MockProvider


# ─── helpers ────────────────────────────────────────────────


async def _call(p: MockProvider, user_text: str):
    return await p.create_message(
        model="mock",
        messages=[{"role": "user", "content": user_text}],
        system="",
        tools=[],
        max_tokens=100,
        temperature=0.7,
    )
