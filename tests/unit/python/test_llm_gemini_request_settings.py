"""The Gemini adapter on Gemini 3.x: thinking level, temperature, cache counts.

* **Thinking.** Gemini 3.x sets how much a model thinks with
  ``thinking_level`` (``minimal``, ``low``, ``medium``, ``high``; not every
  model takes every level). ``thinking_budget`` is the Gemini 2.5 control,
  still accepted for backwards compatibility; a request that sends both is
  rejected, so the adapter sends the level alone.
* **Per call.** The thinking setting is the calling alias's, which
  ``LLMClient`` hands over per call, so a ``fast`` lane that rides a
  ``quality`` seat's provider gets fast's level, not quality's. Settings
  that build the client (``project`` and ``location`` for Vertex AI) stay
  the ones the provider was built with.
* **Temperature.** Google deprecated the sampling parameters for Gemini 3.x
  and asks callers to leave temperature at its default; values below 1.0
  can make the model loop. The adapter sends it only to Gemini 1.x, 2.x and
  Gemma models.
* **Cache counts.** Gemini caches repeated prompt prefixes on its own and
  reports the tokens it read as ``cached_content_token_count``, which
  ``prompt_token_count`` includes. They are billed at a tenth of the input
  price, so the adapter counts them apart from the uncached input.
"""

from __future__ import annotations

import logging
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from agents import llm_gemini
from agents.llm_gemini import GeminiProvider
from agents.llm_types import Usage

from ._gemini_test_helpers import _gemini_response, _make_gemini_provider, _mock_genai_modules


@pytest.fixture(autouse=True)
def _nothing_warned_yet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_gemini, "_warned_no_temperature", set())
    monkeypatch.setattr(llm_gemini, "_warned_level_and_budget", set())


async def _config(
    provider: GeminiProvider, model: str = "gemini-3.8-flash", **extra: Any,
) -> dict[str, Any]:
    """The generation config the adapter hands the SDK."""
    gc = AsyncMock(return_value=_gemini_response())
    provider._client.aio.models.generate_content = gc
    await provider.create_message(
        model=model, messages=[{"role": "user", "content": "hi"}], system="",
        tools=[], max_tokens=64, temperature=0.2, **extra,
    )
    config: dict[str, Any] = gc.call_args.kwargs["config"]
    return config


class TestThinking:
    async def test_thinking_level_goes_in_thinking_config(self) -> None:
        config = await _config(_make_gemini_provider({"thinking_level": "low"}))
        assert config["thinking_config"] == {"thinking_level": "low"}

    async def test_a_level_and_a_budget_together_send_only_the_level(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        provider = _make_gemini_provider({"thinking_level": "minimal", "thinking_budget": 0})
        with caplog.at_level(logging.WARNING, logger="agents.llm_gemini"):
            config = await _config(provider, "gemini-3.5-flash-lite")
            await _config(provider, "gemini-3.5-flash-lite")
        assert config["thinking_config"] == {"thinking_level": "minimal"}
        assert len([r for r in caplog.records if "thinking_budget" in r.getMessage()]) == 1

    async def test_the_calls_alias_settings_replace_the_built_ones(self) -> None:
        provider = _make_gemini_provider({"thinking_budget": 0})
        assert "thinking_config" not in await _config(provider, provider_config={})
        config = await _config(provider, provider_config={"thinking_level": "minimal"})
        assert config["thinking_config"] == {"thinking_level": "minimal"}

    async def test_the_built_vertex_settings_survive_a_calls_settings(self) -> None:
        google_mod, genai_mod, client = _mock_genai_modules()
        with patch.dict(sys.modules, {"google": google_mod, "google.genai": genai_mod}):
            provider = GeminiProvider(
                api_key=None, provider_config={"project": "p", "location": "us-central1"},
            )
        client.aio.models.generate_content = AsyncMock(return_value=_gemini_response())
        await provider.create_message(
            model="gemini-3.8-flash", messages=[], system="", tools=[], max_tokens=64,
            temperature=1.0, provider_config={"thinking_level": "low"},
        )
        assert genai_mod.Client.call_args.kwargs == {
            "vertexai": True, "project": "p", "location": "us-central1",
        }

    def test_the_provider_says_it_takes_per_call_settings(self) -> None:
        assert GeminiProvider.accepts_provider_config is True


class TestTemperature:
    @pytest.mark.parametrize("model", ["gemini-3.8-flash", "gemini-3.5-flash-lite"])
    async def test_gemini_3_gets_no_temperature(
        self, model: str, caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="agents.llm_gemini"):
            config = await _config(_make_gemini_provider(), model)
            await _config(_make_gemini_provider(), model)
        assert "temperature" not in config
        assert len([r for r in caplog.records if model in r.getMessage()]) == 1

    @pytest.mark.parametrize("model", ["gemini-2.5-flash", "gemini-1.5-pro", "gemma-3-27b-it"])
    async def test_older_gemini_and_gemma_keep_their_temperature(self, model: str) -> None:
        assert (await _config(_make_gemini_provider(), model))["temperature"] == 0.2

    @pytest.mark.parametrize(
        "model", ["models/gemini-2.5-flash", "publishers/google/models/gemini-2.0-flash"],
    )
    async def test_a_model_named_by_its_path_keeps_its_temperature(self, model: str) -> None:
        # The Gemini API and Vertex AI also take a model by its resource path.
        assert (await _config(_make_gemini_provider(), model))["temperature"] == 0.2

    async def test_a_gemini_3_model_named_by_its_path_gets_no_temperature(self) -> None:
        assert "temperature" not in await _config(
            _make_gemini_provider(), "models/gemini-3.8-flash",
        )


class TestCacheCounts:
    async def _usage(self, **meta: Any) -> Usage:
        provider = _make_gemini_provider()
        response = _gemini_response()
        response.usage_metadata = SimpleNamespace(**meta)
        provider._client.aio.models.generate_content = AsyncMock(return_value=response)
        result = await provider.create_message(
            model="gemini-3.8-flash", messages=[], system="", tools=[], max_tokens=64,
            temperature=1.0,
        )
        return result.usage

    async def test_cached_tokens_are_counted_apart_from_the_uncached_input(self) -> None:
        usage = await self._usage(
            prompt_token_count=5000, cached_content_token_count=4096,
            candidates_token_count=40, thoughts_token_count=10,
        )
        assert usage == Usage(
            input_tokens=904, output_tokens=50, cache_write_tokens=0, cache_read_tokens=4096,
        )

    async def test_no_cache_hit_counts_all_input_as_uncached(self) -> None:
        usage = await self._usage(
            prompt_token_count=500, cached_content_token_count=None, candidates_token_count=5,
        )
        assert usage == Usage(input_tokens=500, output_tokens=5)
