"""Tests for the offline / mock LLM provider (agents.llm_offline).

No network, no API key, no cost — these tests assert that the MockProvider
returns deterministic scripted or fallback text and that ``create_provider``
routes to it the **same standard way** every other provider is selected:
through the resolved ``provider`` field (an alias declaring ``provider:
mock``, or a per-agent ``provider: mock``). There is no global env force-knob
— RFC 0033 made provider selection purely config/alias-driven (the v0.3.4
provider-parity refactor removed ``PERSATRIX_OFFLINE``).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from agents.llm_client import MockProvider as MockProviderReexport
from agents.llm_client import create_provider
from agents.llm_offline import MockProvider, reset_cache
from agents.llm_types import StopReason
from agents.model_aliases import use_alias_map

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
    """Deterministic baseline: fixtures pointed at a temp file.

    Mirrors conftest's autouse env-isolation idiom so
    PERSATRIX_OFFLINE_RESPONSES never leaks across tests, and the cached
    fixture read is cleared before and after each test. (The responses path
    is mock *configuration* — analogous to an API key — not a provider-
    selection knob.)
    """
    fixture = tmp_path / "offline_responses.yaml"
    fixture.write_text(_FIXTURE, encoding="utf-8")
    monkeypatch.setenv("PERSATRIX_OFFLINE_RESPONSES", str(fixture))
    reset_cache()
    yield
    reset_cache()


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


def test_create_provider_alias_routes_to_mock() -> None:
    """An alias declaring ``provider: mock`` routes to MockProvider — the
    same standard alias path anthropic / openai / ollama take. The alias's
    physical ``model`` reaches the call site (never the alias name)."""
    alias_map = {
        "quality": {
            "provider": "mock",
            "model": "offline",
            "input_per_1m_tokens": 0,
            "output_per_1m_tokens": 0,
        },
    }
    with use_alias_map(alias_map):
        provider, model = create_provider({"id": "ember-owl", "model": "quality"})
    assert isinstance(provider, MockProvider)
    assert provider.name == "mock"
    assert model == "offline"


# A configured anthropic alias for the "real provider is used" cases — as of
# RFC 0033 Phase 3 a raw vendor ID is rejected, so these route via an alias.
_ANTHROPIC_ALIAS = {
    "quality": {
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "input_per_1m_tokens": 3.0,
        "output_per_1m_tokens": 15.0,
    },
}


def test_create_provider_agreeing_mock_provider_field() -> None:
    """A redundant-but-agreeing per-agent ``provider: mock`` field on a mock
    alias is accepted (RFC 0033 §D rule 1) and routes to MockProvider."""
    alias_map = {
        "offline": {
            "provider": "mock",
            "model": "mock",
            "input_per_1m_tokens": 0,
            "output_per_1m_tokens": 0,
        },
    }
    with use_alias_map(alias_map):
        provider, _model = create_provider(
            {"id": "x", "model": "offline", "provider": "mock"}
        )
    assert isinstance(provider, MockProvider)


def test_create_provider_per_agent_mock_raw_model_id_is_rejected() -> None:
    """ISSUE-0074 / RFC 0033 Phase 3 — a single-agent ``provider: mock`` opt-in
    that names a *raw* vendor model id is rejected with a loud ``SystemExit``.

    Pre-Phase-3 the raw-id mock path merely emitted a deprecation warning and
    nudged the ``persatrix.llm.alias.raw_id_usage`` gate counter off zero (the
    open question this issue raised). Phase 3 retired the §E raw-vendor-ID
    pass-through entirely, so the question is now decided: a mock agent, like
    every other, must reference a declared ``models.aliases`` entry — there is
    no raw-id escape hatch and no counter to nudge. The resolver rejects the
    unknown reference before the per-agent ``provider: mock`` field is ever
    consulted, naming the string and pointing at the one place to declare it.
    """
    # A valid map that simply does not declare the raw id the agent names.
    alias_map = {
        "offline": {
            "provider": "mock",
            "model": "mock",
            "input_per_1m_tokens": 0,
            "output_per_1m_tokens": 0,
        },
    }
    with use_alias_map(alias_map):
        with pytest.raises(SystemExit, match="not a declared alias"):
            create_provider(
                {
                    "id": "x",
                    "model": "claude-haiku-4-5-20251001",
                    "provider": "mock",
                }
            )


def test_create_provider_offline_env_does_not_force_mock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The removed ``PERSATRIX_OFFLINE`` knob no longer forces the mock.

    Provider selection is config-driven now: with the agent routed to a real
    cloud model and no mock alias/override, setting the legacy env has no
    effect — the agent resolves to its configured provider.
    """
    monkeypatch.setenv("PERSATRIX_OFFLINE", "1")
    with use_alias_map(_ANTHROPIC_ALIAS):
        provider, _model = create_provider({"id": "ember-owl", "model": "quality"})
    assert not isinstance(provider, MockProvider)
    assert provider.name == "anthropic"


def test_create_provider_normal_path_unaffected() -> None:
    """With no provider override, the real SDK provider is used and the
    alias's physical model id reaches the call site."""
    with use_alias_map(_ANTHROPIC_ALIAS):
        provider, model = create_provider({"id": "x", "model": "quality"})
    assert not isinstance(provider, MockProvider)
    assert provider.name == "anthropic"
    assert model == "claude-sonnet-4-6"


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
