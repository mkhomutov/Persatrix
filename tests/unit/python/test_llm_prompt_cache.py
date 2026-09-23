"""Prompt caching in the Anthropic adapter (EXP-001 harness PR 3).

EXP-001's arm D′ carries the full transcripts of a series' earlier meetings
in a prompt prefix the provider caches. Pre-registration §3, check 3: the
first call that carries the prefix writes it to the cache, later calls read
it, and no other arm sets a cache breakpoint. Check 4: every call's record
includes its cache reads and writes.

So a caller hands the stable text to ``LLMClient.create_message`` as
``cache_prefix``. The Anthropic adapter sends it as the first system block,
marked for the cache; a call without it carries no cache marker at all. A
provider that cannot cache gets the prefix joined onto the front of the
system prompt, so the model still reads the same words.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.llm_client import (
    AnthropicProvider,
    LLMClient,
    LLMResponse,
    Usage,
    _estimate_input_tokens,
)

_PREFIX = "Transcript of the briefing, 6 October 2036: ..."


def _anthropic_provider() -> AnthropicProvider:
    sdk = MagicMock()
    sdk.AsyncAnthropic.return_value = AsyncMock()
    with patch.dict(sys.modules, {"anthropic": sdk}):
        provider = AnthropicProvider(api_key="test-key")
    provider._client = AsyncMock()
    return provider


def _response(**usage: int | None) -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text="ok")],
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=10, output_tokens=5, **usage),
    )


async def _call(provider: object, **extra: object) -> LLMResponse:
    return await provider.create_message(  # type: ignore[attr-defined]
        model="claude-sonnet-4-6",
        messages=[{"role": "user", "content": "hi"}],
        system="You are an adviser.",
        tools=[],
        max_tokens=100,
        temperature=0.7,
        **extra,
    )


def _has_cache_control(value: object) -> bool:
    if isinstance(value, dict):
        return "cache_control" in value or any(_has_cache_control(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_cache_control(v) for v in value)
    return False


class TestAnthropicCacheMarker:
    async def test_prefix_is_the_first_system_block_and_marked_for_the_cache(self):
        provider = _anthropic_provider()
        provider._client.messages.create = AsyncMock(return_value=_response())
        await _call(provider, cache_prefix=_PREFIX)
        sent = provider._client.messages.create.call_args.kwargs
        assert sent["system"] == [
            {"type": "text", "text": _PREFIX, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "You are an adviser."},
        ]

    async def test_prefix_without_a_system_prompt_is_the_only_block(self):
        provider = _anthropic_provider()
        provider._client.messages.create = AsyncMock(return_value=_response())
        await provider.create_message(
            model="claude-sonnet-4-6", messages=[], system="", tools=[],
            max_tokens=100, temperature=0.7, cache_prefix=_PREFIX,
        )
        sent = provider._client.messages.create.call_args.kwargs
        assert sent["system"] == [
            {"type": "text", "text": _PREFIX, "cache_control": {"type": "ephemeral"}},
        ]

    async def test_no_prefix_sends_no_cache_marker_anywhere(self):
        provider = _anthropic_provider()
        provider._client.messages.create = AsyncMock(return_value=_response())
        await _call(provider)
        sent = provider._client.messages.create.call_args.kwargs
        assert sent["system"] == "You are an adviser."
        assert not _has_cache_control(sent)


class TestCacheTokenCounts:
    async def test_cache_writes_and_reads_are_counted(self):
        provider = _anthropic_provider()
        provider._client.messages.create = AsyncMock(
            return_value=_response(cache_creation_input_tokens=2048, cache_read_input_tokens=512),
        )
        resp = await _call(provider, cache_prefix=_PREFIX)
        assert resp.usage == Usage(
            input_tokens=10, output_tokens=5, cache_write_tokens=2048, cache_read_tokens=512,
        )

    @pytest.mark.parametrize("usage", [{}, {"cache_creation_input_tokens": None,
                                            "cache_read_input_tokens": None}])
    async def test_missing_or_null_cache_counts_are_zero(self, usage):
        provider = _anthropic_provider()
        provider._client.messages.create = AsyncMock(return_value=_response(**usage))
        resp = await _call(provider)
        assert (resp.usage.cache_write_tokens, resp.usage.cache_read_tokens) == (0, 0)

    def test_usage_defaults_keep_two_argument_construction(self):
        assert Usage(3, 4) == Usage(input_tokens=3, output_tokens=4,
                                    cache_write_tokens=0, cache_read_tokens=0)


class TestClientPassesThePrefix:
    async def test_a_caching_provider_receives_the_prefix_unchanged(self):
        provider = _anthropic_provider()
        provider._client.messages.create = AsyncMock(return_value=_response())
        await _call(LLMClient(provider), cache_prefix=_PREFIX)
        sent = provider._client.messages.create.call_args.kwargs
        assert sent["system"][0]["text"] == _PREFIX

    async def test_a_provider_that_cannot_cache_reads_the_prefix_in_its_system_prompt(self):
        # An AsyncMock answers every attribute with a truthy mock, so this
        # also proves the client asks for a real True, not any truthy value.
        provider = AsyncMock()
        provider.create_message.return_value = LLMResponse(text="ok")
        await _call(LLMClient(provider), cache_prefix=_PREFIX)
        sent = provider.create_message.call_args.kwargs
        assert "cache_prefix" not in sent
        assert sent["system"] == f"{_PREFIX}\n\nYou are an adviser."

    async def test_no_prefix_leaves_the_call_as_it_was(self):
        provider = AsyncMock()
        provider.create_message.return_value = LLMResponse(text="ok")
        await _call(LLMClient(provider))
        sent = provider.create_message.call_args.kwargs
        assert "cache_prefix" not in sent
        assert sent["system"] == "You are an adviser."

    def test_the_lease_estimate_counts_the_prefix(self):
        without = _estimate_input_tokens({"system": "You are an adviser.", "messages": []})
        with_prefix = _estimate_input_tokens(
            {"system": "You are an adviser.", "messages": [], "cache_prefix": _PREFIX * 20},
        )
        assert with_prefix > without
