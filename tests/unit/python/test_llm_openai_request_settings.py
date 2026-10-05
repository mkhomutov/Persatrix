"""The OpenAI adapter's request fields for current models, and its cache counts.

OpenAI's current models reason, and three request fields follow from it:

* the reply cap is ``max_completion_tokens``, which counts reasoning tokens
  too; ``max_tokens`` is deprecated and reasoning models reject it. A server
  that only speaks the OpenAI wire format (Ollama, vLLM, LM Studio: any
  ``base_url``) keeps getting ``max_tokens``, the field those servers read;
* an alias's ``provider_config.reasoning_effort`` goes in as
  ``reasoning_effort``. GPT-6 Sol and Luna take tool calls on Chat
  Completions only at ``none``;
* ``temperature`` reaches OpenAI only where it is accepted: the GPT-4
  families, or any model told to reason at ``none``. A reasoning model
  rejects it with a 400. A compatible server still gets it.

OpenAI caches prompts on its own and reports what it read
(``prompt_tokens_details.cached_tokens``) and, from GPT-5.6 on, what it wrote
(``cache_write_tokens``, billed at 1.25 times input). ``prompt_tokens``
includes both, so the adapter counts them apart from the uncached input, the
way the Anthropic adapter does, and the three still add up to the prompt.
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
from agents.llm_ollama import OllamaProvider
from agents.llm_providers import OpenAIProvider
from agents.llm_types import Usage
from agents.model_aliases import use_alias_map


@pytest.fixture(autouse=True)
def _no_model_warned_yet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_providers, "_warned_no_openai_temperature", set())


def _completion(usage: Any = None) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="ok", tool_calls=None),
                finish_reason="stop",
            ),
        ],
        usage=usage,
    )


def _provider(
    base_url: str | None = None, provider_config: dict[str, Any] | None = None,
) -> OpenAIProvider:
    sdk = MagicMock()
    sdk.AsyncOpenAI.return_value = AsyncMock()
    with patch.dict(sys.modules, {"openai": sdk}):
        provider = OpenAIProvider(
            api_key="test-key", base_url=base_url, provider_config=provider_config,
        )
    client: Any = AsyncMock()
    client.chat.completions.create = AsyncMock(return_value=_completion())
    provider._client = client
    return provider


async def _sent(provider: OpenAIProvider, model: str, **extra: Any) -> dict[str, Any]:
    await provider.create_message(
        model=model,
        messages=[{"role": "user", "content": "hi"}],
        system="",
        tools=[],
        max_tokens=256,
        temperature=0.7,
        **extra,
    )
    client: Any = provider._client
    sent: dict[str, Any] = client.chat.completions.create.call_args.kwargs
    return sent


class TestTheReplyCap:
    async def test_openai_itself_gets_max_completion_tokens(self) -> None:
        sent = await _sent(_provider(), "gpt-6-sol")
        assert sent["max_completion_tokens"] == 256
        assert "max_tokens" not in sent

    async def test_a_compatible_server_keeps_max_tokens(self) -> None:
        sent = await _sent(_provider(base_url="http://localhost:8000/v1"), "my-model")
        assert sent["max_tokens"] == 256
        assert "max_completion_tokens" not in sent

    async def test_ollama_keeps_max_tokens(self) -> None:
        sdk = MagicMock()
        sdk.AsyncOpenAI.return_value = AsyncMock()
        with patch.dict(sys.modules, {"openai": sdk}):
            provider = OllamaProvider()
        client: Any = AsyncMock()
        client.chat.completions.create = AsyncMock(return_value=_completion())
        provider._client = client
        sent = await _sent(provider, "llama3.2")
        assert sent["max_tokens"] == 256
        assert sent["temperature"] == 0.7


class TestReasoningEffortAndTemperature:
    async def test_the_aliases_reasoning_effort_is_sent(self) -> None:
        sent = await _sent(
            _provider(), "gpt-6-sol", provider_config={"reasoning_effort": "none"},
        )
        assert sent["reasoning_effort"] == "none"

    async def test_no_reasoning_effort_sends_none_of_it(self) -> None:
        sent = await _sent(_provider(), "gpt-4o", provider_config={})
        assert "reasoning_effort" not in sent

    @pytest.mark.parametrize("model", ["gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini"])
    async def test_the_gpt_4_families_keep_their_temperature(self, model: str) -> None:
        assert (await _sent(_provider(), model))["temperature"] == 0.7

    async def test_a_model_reasoning_at_none_keeps_its_temperature(self) -> None:
        sent = await _sent(
            _provider(), "gpt-6-luna", provider_config={"reasoning_effort": "none"},
        )
        assert sent["temperature"] == 0.7

    @pytest.mark.parametrize("effort", [None, "low", "medium"])
    async def test_a_reasoning_model_gets_no_temperature(
        self, effort: str | None, caplog: pytest.LogCaptureFixture,
    ) -> None:
        config = {} if effort is None else {"reasoning_effort": effort}
        with caplog.at_level(logging.WARNING, logger="agents.llm_providers"):
            sent = await _sent(_provider(), "gpt-6-sol", provider_config=config)
            await _sent(_provider(), "gpt-6-sol", provider_config=config)
        assert "temperature" not in sent
        warnings = [r for r in caplog.records if "gpt-6-sol" in r.getMessage()]
        assert len(warnings) == 1

    async def test_a_compatible_server_always_gets_temperature(self) -> None:
        sent = await _sent(_provider(base_url="http://localhost:8000/v1"), "qwen3")
        assert sent["temperature"] == 0.7

    async def test_the_built_settings_apply_without_a_call_alias(self) -> None:
        sent = await _sent(_provider(provider_config={"reasoning_effort": "low"}), "gpt-6-sol")
        assert sent["reasoning_effort"] == "low"

    async def test_the_calls_alias_settings_replace_the_built_ones(self) -> None:
        provider = _provider(provider_config={"reasoning_effort": "low"})
        sent = await _sent(provider, "gpt-6-luna", provider_config={"reasoning_effort": "none"})
        assert sent["reasoning_effort"] == "none"

    async def test_the_factory_builds_the_provider_with_its_alias_settings(self) -> None:
        aliases = {
            "quality": {
                "provider": "openai",
                "model": "gpt-6-sol",
                "input_per_1m_tokens": 2.0,
                "output_per_1m_tokens": 10.0,
                "provider_config": {"reasoning_effort": "none"},
            },
        }
        sdk = MagicMock()
        sdk.AsyncOpenAI.return_value = AsyncMock()
        with use_alias_map(aliases), patch.dict(sys.modules, {"openai": sdk}):
            provider, model = create_provider({"id": "q", "model": "quality"})
        assert isinstance(provider, OpenAIProvider)
        client: Any = AsyncMock()
        client.chat.completions.create = AsyncMock(return_value=_completion())
        provider._client = client
        sent = await _sent(provider, model)
        assert sent["reasoning_effort"] == "none"
        assert sent["temperature"] == 0.7


class TestCacheCounts:
    async def _usage(self, usage: Any) -> Usage:
        provider = _provider()
        client: Any = provider._client
        client.chat.completions.create = AsyncMock(return_value=_completion(usage))
        response = await provider.create_message(
            model="gpt-6-sol", messages=[], system="", tools=[], max_tokens=1, temperature=1.0,
        )
        return response.usage

    async def test_cached_and_written_tokens_are_counted_apart(self) -> None:
        usage = SimpleNamespace(
            prompt_tokens=3000,
            completion_tokens=50,
            prompt_tokens_details=SimpleNamespace(cached_tokens=2048, cache_write_tokens=900),
        )
        assert await self._usage(usage) == Usage(
            input_tokens=52, output_tokens=50, cache_write_tokens=900, cache_read_tokens=2048,
        )

    async def test_older_responses_without_details_count_all_input_as_uncached(self) -> None:
        usage = SimpleNamespace(prompt_tokens=100, completion_tokens=5)
        assert await self._usage(usage) == Usage(input_tokens=100, output_tokens=5)

    async def test_null_details_and_counts_read_as_zero(self) -> None:
        usage = SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=5,
            prompt_tokens_details=SimpleNamespace(cached_tokens=None, cache_write_tokens=None),
        )
        assert await self._usage(usage) == Usage(input_tokens=100, output_tokens=5)

    async def test_a_server_that_reports_null_counts_reads_as_zero(self) -> None:
        # Some OpenAI-compatible servers send a usage block with null counts.
        usage = SimpleNamespace(prompt_tokens=None, completion_tokens=None)
        assert await self._usage(usage) == Usage(input_tokens=0, output_tokens=0)

    async def test_uncached_input_never_goes_below_zero(self) -> None:
        usage = SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=5,
            prompt_tokens_details=SimpleNamespace(cached_tokens=90, cache_write_tokens=20),
        )
        assert (await self._usage(usage)).input_tokens == 0
