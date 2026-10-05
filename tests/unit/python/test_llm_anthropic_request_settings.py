"""The Anthropic adapter's per-alias request settings (ISSUE-0169).

Claude Sonnet 5.5 and the other current models think by default, and
thinking spends the same ``max_tokens`` as the reply. An alias's
``provider_config`` can say how much thinking a call gets (``effort``,
``thinking``) and whether its prompt goes to the provider's cache
(``prompt_cache``). An alias that says nothing gets a request with none of
these fields, exactly as before: EXP-001's arms run on such an alias, and
its pre-registration allows no cache marker on any arm but D′.

The settings are the ones the call's alias carries (``LLMClient`` hands them
over per call), else the ones the provider was built with.
"""

from __future__ import annotations

import logging
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents import llm_providers
from agents.llm_factory import create_provider
from agents.llm_providers import AnthropicProvider
from agents.model_aliases import use_alias_map

_PREFIX = "Transcript of the briefing: ..."


@pytest.fixture(autouse=True)
def _no_model_warned_yet(monkeypatch: pytest.MonkeyPatch) -> None:
    # The adapter warns once per model per process; each test starts fresh.
    monkeypatch.setattr(llm_providers, "_warned_no_temperature", set())


def _provider(provider_config: dict[str, Any] | None = None) -> AnthropicProvider:
    sdk = MagicMock()
    sdk.AsyncAnthropic.return_value = AsyncMock()
    with patch.dict(sys.modules, {"anthropic": sdk}):
        provider = AnthropicProvider(api_key="test-key", provider_config=provider_config)
    client: Any = AsyncMock()
    client.messages.create = AsyncMock(
        return_value=SimpleNamespace(
            content=[SimpleNamespace(type="text", text="ok")],
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        ),
    )
    provider._client = client
    return provider


async def _sent(provider: AnthropicProvider, **extra: Any) -> dict[str, Any]:
    """The keyword arguments the adapter hands the SDK."""
    await provider.create_message(
        model=extra.pop("model", "claude-sonnet-5-5"),
        messages=[{"role": "user", "content": "hi"}],
        system=extra.pop("system", "You are an adviser."),
        tools=extra.pop("tools", []),
        max_tokens=4096,
        temperature=0.7,
        **extra,
    )
    client: Any = provider._client
    sent: dict[str, Any] = client.messages.create.call_args.kwargs
    return sent


def _has_cache_control(value: object) -> bool:
    if isinstance(value, dict):
        return "cache_control" in value or any(_has_cache_control(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_cache_control(v) for v in value)
    return False


class TestNoSettingsChangesNothing:
    async def test_an_alias_without_settings_sends_no_thinking_effort_or_cache(self) -> None:
        sent = await _sent(_provider(), provider_config={})
        assert "thinking" not in sent
        assert "output_config" not in sent
        assert not _has_cache_control(sent)
        assert sent["system"] == "You are an adviser."

    async def test_construction_settings_unrelated_to_requests_change_nothing(self) -> None:
        sent = await _sent(_provider({"notes": "a comment, not a setting"}))
        assert "thinking" not in sent
        assert "output_config" not in sent
        assert not _has_cache_control(sent)


class TestThinkingAndEffort:
    @pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
    async def test_effort_goes_in_output_config(self, effort: str) -> None:
        sent = await _sent(_provider(), provider_config={"effort": effort})
        assert sent["output_config"] == {"effort": effort}

    @pytest.mark.parametrize("thinking", ["adaptive", "disabled", "between_tools"])
    async def test_thinking_names_its_type(self, thinking: str) -> None:
        sent = await _sent(_provider(), provider_config={"thinking": thinking})
        assert sent["thinking"] == {"type": thinking}

    async def test_the_settings_the_provider_was_built_with_apply_without_a_call_alias(
        self,
    ) -> None:
        sent = await _sent(_provider({"effort": "medium", "thinking": "adaptive"}))
        assert sent["output_config"] == {"effort": "medium"}
        assert sent["thinking"] == {"type": "adaptive"}

    async def test_the_calls_alias_settings_replace_the_built_ones_whole(self) -> None:
        # A `fast` lane riding a `quality` seat's provider: fast says nothing,
        # so the call carries nothing, not quality's effort.
        sent = await _sent(_provider({"effort": "low"}), provider_config={})
        assert "output_config" not in sent

    async def test_the_factory_builds_the_provider_with_its_alias_settings(self) -> None:
        aliases = {
            "quality": {
                "provider": "anthropic",
                "model": "claude-sonnet-5-5",
                "input_per_1m_tokens": 2.0,
                "output_per_1m_tokens": 10.0,
                "provider_config": {"effort": "low"},
            },
        }
        sdk = MagicMock()
        sdk.AsyncAnthropic.return_value = AsyncMock()
        with use_alias_map(aliases), patch.dict(sys.modules, {"anthropic": sdk}):
            provider, model = create_provider({"id": "q", "model": "quality"})
        assert isinstance(provider, AnthropicProvider)
        client: Any = AsyncMock()
        client.messages.create = AsyncMock(
            return_value=SimpleNamespace(
                content=[], stop_reason="end_turn",
                usage=SimpleNamespace(input_tokens=1, output_tokens=1),
            ),
        )
        provider._client = client
        sent = await _sent(provider)
        assert sent["output_config"] == {"effort": "low"}


class TestThinkingAndTemperature:
    """Anthropic takes a temperature only from a model that is not thinking.
    An alias that turns thinking on therefore sends none, even to a model
    family that otherwise takes one, and the adapter logs it once."""

    @pytest.mark.parametrize("thinking", ["adaptive", "between_tools"])
    async def test_a_model_told_to_think_gets_no_temperature(
        self, thinking: str, caplog: pytest.LogCaptureFixture,
    ) -> None:
        config = {"thinking": thinking}
        with caplog.at_level(logging.WARNING, logger="agents.llm_providers"):
            sent = await _sent(_provider(), model="claude-sonnet-4-6", provider_config=config)
            await _sent(_provider(), model="claude-sonnet-4-6", provider_config=config)
        assert "temperature" not in sent
        assert sent["thinking"] == {"type": thinking}
        dropped = [r.getMessage() for r in caplog.records if "claude-sonnet-4-6" in r.getMessage()]
        assert len(dropped) == 1
        assert "thinking" in dropped[0]

    @pytest.mark.parametrize("config", [{}, {"thinking": "disabled"}, {"effort": "low"}])
    async def test_a_model_that_is_not_thinking_keeps_its_temperature(
        self, config: dict[str, Any],
    ) -> None:
        sent = await _sent(_provider(), model="claude-sonnet-4-6", provider_config=config)
        assert sent["temperature"] == 0.7


class TestPromptCache:
    async def test_caching_marks_the_system_prompt_and_the_conversation(self) -> None:
        sent = await _sent(_provider(), provider_config={"prompt_cache": True})
        assert sent["cache_control"] == {"type": "ephemeral"}
        assert sent["system"] == [
            {
                "type": "text",
                "text": "You are an adviser.",
                "cache_control": {"type": "ephemeral"},
            },
        ]

    async def test_caching_with_a_prefix_marks_both_system_blocks(self) -> None:
        sent = await _sent(
            _provider(), provider_config={"prompt_cache": True}, cache_prefix=_PREFIX,
        )
        assert sent["system"] == [
            {"type": "text", "text": _PREFIX, "cache_control": {"type": "ephemeral"}},
            {
                "type": "text",
                "text": "You are an adviser.",
                "cache_control": {"type": "ephemeral"},
            },
        ]
        assert sent["cache_control"] == {"type": "ephemeral"}

    async def test_caching_without_a_system_prompt_marks_only_the_conversation(self) -> None:
        sent = await _sent(_provider(), provider_config={"prompt_cache": True}, system="")
        assert "system" not in sent
        assert sent["cache_control"] == {"type": "ephemeral"}

    async def test_caching_marks_the_last_tool_and_leaves_the_callers_list_alone(self) -> None:
        # Tools come first in the prompt and are the same on every call an
        # agent makes, so a marker after them is read across turns.
        tools = [
            {"name": "a", "description": "A.", "input_schema": {"type": "object"}},
            {"name": "b", "description": "B.", "input_schema": {"type": "object"}},
        ]
        sent = await _sent(_provider(), provider_config={"prompt_cache": True}, tools=tools)
        assert sent["tools"][0] == tools[0]
        assert sent["tools"][1] == {**tools[1], "cache_control": {"type": "ephemeral"}}
        assert "cache_control" not in tools[1]

    async def test_tools_carry_no_marker_without_caching(self) -> None:
        tools = [{"name": "a", "description": "A.", "input_schema": {"type": "object"}}]
        sent = await _sent(_provider(), provider_config={}, tools=tools)
        assert sent["tools"] == tools

    async def test_a_prefix_without_caching_keeps_its_single_marker(self) -> None:
        # EXP-001 arm D′: the prefix is marked, nothing else is.
        sent = await _sent(_provider(), provider_config={}, cache_prefix=_PREFIX)
        assert sent["system"] == [
            {"type": "text", "text": _PREFIX, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "You are an adviser."},
        ]
        assert "cache_control" not in sent

    @pytest.mark.parametrize("value", [False, None, 0, "", "false"])
    async def test_only_true_turns_caching_on(self, value: object) -> None:
        sent = await _sent(_provider(), provider_config={"prompt_cache": value})
        assert not _has_cache_control(sent)
