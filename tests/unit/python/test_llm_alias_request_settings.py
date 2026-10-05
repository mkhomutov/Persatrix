"""Per-call request settings from the alias a call names (ISSUE-0169 item 1).

An alias's ``provider_config`` can carry settings a provider applies to each
request it sends: Anthropic's thinking, effort and prompt caching, OpenAI's
reasoning effort, Gemini's thinking level. A persona holds one client, built
from its own seat alias, and a lane on the same vendor (the ``fast`` bid, the
``summarizer`` close) rides that client. So a setting fixed when the provider
is built would reach every call the persona makes, the lanes' included:
``thinking_level: minimal`` on ``fast`` would never reach a bid made through a
``quality`` seat, and ``effort: low`` on ``quality`` would reach Haiku, which
rejects effort.

``LLMClient`` therefore resolves the alias each call names and hands its
``provider_config`` to a provider that says it takes one
(``accepts_provider_config is True``). The provider applies that call's
settings in place of the ones it was built with. A call that names no alias
sends nothing, and the provider keeps the settings it was built with.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import yaml

from agents.llm_client import LLMClient
from agents.llm_providers import AnthropicProvider
from agents.llm_types import LLMResponse
from agents.model_aliases import use_alias_map
from agents.server_persona import load_agent

_ALIASES: dict[str, dict[str, Any]] = {
    "quality": {
        "provider": "anthropic",
        "model": "claude-sonnet-5-5",
        "input_per_1m_tokens": 2.0,
        "output_per_1m_tokens": 10.0,
        "provider_config": {"effort": "low"},
    },
    "fast": {
        "provider": "anthropic",
        "model": "claude-haiku-4-5",
        "input_per_1m_tokens": 1.0,
        "output_per_1m_tokens": 5.0,
    },
}


class _RecordingAnthropic(AnthropicProvider):
    """A factory-class provider that records the keyword arguments it is
    called with instead of contacting the vendor."""

    def __init__(self) -> None:
        sdk = MagicMock()
        sdk.AsyncAnthropic.return_value = AsyncMock()
        with patch.dict(sys.modules, {"anthropic": sdk}):
            super().__init__(api_key="test-key")
        self.calls: list[dict[str, Any]] = []

    async def create_message(self, **kwargs: Any) -> LLMResponse:  # type: ignore[override]
        self.calls.append(kwargs)
        return LLMResponse(text="ok")


async def _call(client: LLMClient, *, model_alias: str | None) -> None:
    await client.create_message(
        model="claude-sonnet-5-5",
        model_alias=model_alias,
        messages=[{"role": "user", "content": "hi"}],
        system="",
        tools=[],
        max_tokens=64,
        temperature=0.2,
    )


class TestTheCallsAliasSettingsReachTheProvider:
    async def test_the_named_alias_settings_are_handed_over(self) -> None:
        provider = _RecordingAnthropic()
        with use_alias_map(_ALIASES):
            await _call(LLMClient(provider), model_alias="quality")
        assert provider.calls[0]["provider_config"] == {"effort": "low"}

    async def test_a_same_vendor_lane_gets_its_own_alias_settings(self) -> None:
        # The persona's client was built from `quality`; the bid names `fast`,
        # on the same vendor, so it rides the same provider. It must carry
        # fast's settings (none), never quality's effort.
        provider = _RecordingAnthropic()
        with use_alias_map(_ALIASES):
            await _call(LLMClient(provider), model_alias="fast")
        assert provider.calls[0]["provider_config"] == {}

    async def test_a_call_that_names_no_alias_hands_over_nothing(self) -> None:
        provider = _RecordingAnthropic()
        with use_alias_map(_ALIASES):
            await _call(LLMClient(provider), model_alias=None)
        assert "provider_config" not in provider.calls[0]

    async def test_an_alias_that_does_not_resolve_hands_over_nothing(self) -> None:
        provider = _RecordingAnthropic()
        with use_alias_map(_ALIASES):
            await _call(LLMClient(provider), model_alias="no-such-alias")
        assert "provider_config" not in provider.calls[0]

    async def test_the_settings_are_a_copy_the_provider_cannot_change(self) -> None:
        provider = _RecordingAnthropic()
        with use_alias_map(_ALIASES):
            await _call(LLMClient(provider), model_alias="quality")
            provider.calls[0]["provider_config"]["effort"] = "max"
            await _call(LLMClient(provider), model_alias="quality")
        assert provider.calls[1]["provider_config"] == {"effort": "low"}


class TestTheSeatsOwnCallsKeepTheBuiltSettings:
    """The provider was built from the seat alias, with any gap the alias
    left filled from the agent's own ``provider_config`` (RFC 0033 §D rule
    2). A call through the seat alias keeps those settings; only a call
    through another alias gets that alias's."""

    async def test_a_call_through_the_seat_alias_hands_over_nothing(self) -> None:
        provider = _RecordingAnthropic()
        with use_alias_map(_ALIASES):
            await _call(LLMClient(provider, seat_alias="quality"), model_alias="quality")
        assert "provider_config" not in provider.calls[0]

    async def test_a_lane_call_still_gets_its_own_alias_settings(self) -> None:
        provider = _RecordingAnthropic()
        with use_alias_map(_ALIASES):
            await _call(LLMClient(provider, seat_alias="quality"), model_alias="fast")
        assert provider.calls[0]["provider_config"] == {}

    async def test_a_loaded_agent_knows_its_seat(self, tmp_path: Path) -> None:
        provider = _RecordingAnthropic()
        config = tmp_path / "agents.yaml"
        config.write_text(yaml.safe_dump({"schema_version": "0.1", "agents": [{
            "id": "planner", "name": "Planner", "role": "Plans", "model": "quality",
            "type": "task", "instructions": "Plan.", "tools": [], "permissions": {},
        }]}))
        with use_alias_map(_ALIASES), patch(
            "agents.server_persona.create_provider", return_value=(provider, "claude-sonnet-5-5"),
        ), patch("agents.server_persona.model_aliases", return_value=_ALIASES):
            agent = load_agent("planner", str(config), str(tmp_path))
            assert agent._llm_client is not None
            await _call(agent._llm_client, model_alias="quality")
            await _call(agent._llm_client, model_alias="fast")
        assert "provider_config" not in provider.calls[0]
        assert provider.calls[1]["provider_config"] == {}


class TestProvidersThatTakeNoSettingsAreLeftAlone:
    async def test_a_provider_that_does_not_say_it_takes_settings_gets_none(self) -> None:
        # An AsyncMock answers every attribute with a truthy mock, so this
        # also proves the client asks for a real True, not any truthy value.
        provider = AsyncMock()
        provider.name = "anthropic"
        provider.create_message.return_value = LLMResponse(text="ok")
        with use_alias_map(_ALIASES):
            await _call(LLMClient(provider), model_alias="quality")
        assert "provider_config" not in provider.create_message.call_args.kwargs
