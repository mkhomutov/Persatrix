"""
Tests for WorkingMemory — context window management with
priority-weighted retention and automatic summarization.

All tests use mock LLM client — no real API calls.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.llm_client import LLMClient, LLMResponse, Usage
from agents.memory.working import ContextSection, WorkingMemory
from agents.model_aliases import use_alias_map

# ISSUE-0072: compression resolves its model through the RFC 0033 alias layer.
# The shipped base config ships ``summarizer`` as ``provider: unconfigured`` (a
# loud SystemExit), so the suite declares a concrete local model the way an
# operator's config would.
_SUMMARIZER_ALIAS_MAP = {
    "summarizer": {
        "provider": "mock",
        "model": "physical-summarizer-model",
        "input_per_1m_tokens": 0.0,
        "output_per_1m_tokens": 0.0,
    },
    # A second alias proving an operator-chosen compression_model also routes
    # through the alias layer (not just the default).
    "alt": {
        "provider": "mock",
        "model": "physical-alt-model",
        "input_per_1m_tokens": 0.0,
        "output_per_1m_tokens": 0.0,
    },
}


@pytest.fixture(autouse=True)
def _configured_summarizer_alias():
    """Route the ``summarizer`` alias to a local mock model for the module."""
    with use_alias_map(_SUMMARIZER_ALIAS_MAP):
        yield

# ─── Fixtures ───────────────────────────────────────────────


def _make_section(
    name: str = "test",
    content: str = "test content",
    priority: int = 50,
    token_count: int = 100,
    compressible: bool = True,
) -> ContextSection:
    return ContextSection(
        name=name,
        content=content,
        priority=priority,
        token_count=token_count,
        compressible=compressible,
    )


def _make_llm_client(summary: str = "compressed summary") -> LLMClient:
    """Create a mock LLMClient that returns a summary response."""
    mock_provider = AsyncMock()
    mock_provider.create_message = AsyncMock(
        return_value=LLMResponse(text=summary, usage=Usage(10, 20))
    )
    mock_provider.format_tool_definitions = MagicMock(return_value=[])
    mock_provider.append_tool_round = MagicMock()
    return LLMClient(mock_provider)


# ─── WorkingMemory.compress_if_needed ───────────────────────


class TestCompression:
    async def test_compression_resolves_model_through_alias_layer(self):
        """ISSUE-0072: compression routes its model through the RFC 0033 alias
        layer — it sends the resolved *physical* model and threads the alias
        name as ``model_alias`` for the span, never a raw vendor literal."""
        wm = WorkingMemory(max_tokens=100)
        wm.add_section(
            _make_section(
                name="low",
                priority=10,
                token_count=200,
                content="a" * 800,
                compressible=True,
            )
        )
        llm_client = AsyncMock()
        llm_client.create_message = AsyncMock(
            return_value=LLMResponse(text="short", usage=Usage(10, 20))
        )
        await wm.compress_if_needed(llm_client)
        llm_client.create_message.assert_awaited()
        kwargs = llm_client.create_message.call_args.kwargs
        assert kwargs["model"] == "physical-summarizer-model"
        assert kwargs["model_alias"] == "summarizer"

    async def test_no_compression_under_budget(self):
        wm = WorkingMemory(max_tokens=500)
        wm.add_section(_make_section(name="a", token_count=100))
        client = _make_llm_client()
        await wm.compress_if_needed(client)
        # LLM should not have been called
        client._provider.create_message.assert_not_called()

    async def test_compresses_lowest_priority_first(self):
        wm = WorkingMemory(max_tokens=200)
        wm.add_section(
            _make_section(name="high", priority=100, token_count=150, compressible=True)
        )
        wm.add_section(
            _make_section(
                name="low",
                priority=10,
                token_count=150,
                content="a" * 600,  # 600 chars → 150 tokens
                compressible=True,
            )
        )
        # Summary is short → reduces token count
        client = _make_llm_client(summary="short summary")
        await wm.compress_if_needed(client)
        # The low-priority section should have been compressed
        low_section = wm.get_section("low")
        assert low_section is not None
        assert low_section.content == "short summary"

    async def test_non_compressible_sections_skipped(self):
        wm = WorkingMemory(max_tokens=100)
        wm.add_section(
            _make_section(name="system", priority=100, token_count=80, compressible=False)
        )
        wm.add_section(
            _make_section(name="conversation", priority=10, token_count=80, compressible=True)
        )
        client = _make_llm_client(summary="short")
        await wm.compress_if_needed(client)
        # System section should not be touched
        system = wm.get_section("system")
        assert system is not None
        assert system.token_count == 80
        # Conversation should be compressed
        conv = wm.get_section("conversation")
        assert conv is not None
        assert conv.content == "short"

    async def test_compression_failure_is_handled(self):
        wm = WorkingMemory(max_tokens=100)
        wm.add_section(
            _make_section(name="a", priority=10, token_count=200, compressible=True)
        )
        client = _make_llm_client()
        client._provider.create_message = AsyncMock(side_effect=RuntimeError("LLM down"))
        # Should not raise
        await wm.compress_if_needed(client)
        # Section should be unchanged
        assert wm.get_section("a").token_count == 200

    async def test_compression_stops_when_under_budget(self):
        wm = WorkingMemory(max_tokens=250)
        wm.add_section(
            _make_section(
                name="low", priority=10, token_count=150, content="a" * 600, compressible=True
            )
        )
        wm.add_section(
            _make_section(
                name="mid", priority=50, token_count=150, content="b" * 600, compressible=True
            )
        )
        # After compressing "low", total should be under budget
        client = _make_llm_client(summary="short")  # ~1 token
        await wm.compress_if_needed(client)
        # Only the low-priority section should have been compressed
        assert client._provider.create_message.await_count == 1

    async def test_compression_passes_all_required_kwargs(self):
        """Verify that compress_if_needed() passes all kwargs required by LLMProvider protocol."""
        wm = WorkingMemory(max_tokens=100)
        wm.add_section(
            _make_section(name="a", token_count=200, content="a" * 800, compressible=True)
        )
        client = _make_llm_client(summary="short")
        await wm.compress_if_needed(client)
        call_kwargs = client._provider.create_message.call_args.kwargs
        assert "model" in call_kwargs
        assert "temperature" in call_kwargs
        assert "messages" in call_kwargs
        assert "system" in call_kwargs
        assert "tools" in call_kwargs
        assert "max_tokens" in call_kwargs

    async def test_compression_uses_configured_model(self):
        """The configured compression_model alias is resolved to its physical
        model before the provider call (ISSUE-0072: alias-routed, not a raw id).
        ``model_alias`` is telemetry — LLMClient strips it before the provider,
        so the provider sees only the physical id."""
        wm = WorkingMemory(max_tokens=100, compression_model="alt")
        wm.add_section(
            _make_section(name="a", token_count=200, content="a" * 800, compressible=True)
        )
        client = _make_llm_client(summary="short")
        await wm.compress_if_needed(client)
        call_kwargs = client._provider.create_message.call_args.kwargs
        assert call_kwargs["model"] == "physical-alt-model"

    async def test_compression_null_response_preserves_original(self):
        """When LLM returns text=None, the original section is preserved."""
        wm = WorkingMemory(max_tokens=100)
        wm.add_section(
            _make_section(
                name="a",
                priority=10,
                token_count=200,
                content="original",
                compressible=True,
            )
        )
        client = _make_llm_client()
        client._provider.create_message = AsyncMock(
            return_value=LLMResponse(text=None, usage=Usage(10, 0))
        )
        await wm.compress_if_needed(client)
        section = wm.get_section("a")
        assert section is not None
        assert section.content == "original"
        assert section.token_count == 200

    async def test_compression_max_tokens_floor(self):
        """Very small sections get a floor of 64 for max_tokens in the LLM call."""
        wm = WorkingMemory(max_tokens=50)
        wm.add_section(
            _make_section(name="tiny", token_count=100, content="x" * 4, compressible=True)
        )
        client = _make_llm_client(summary="s")
        await wm.compress_if_needed(client)
        call_kwargs = client._provider.create_message.call_args.kwargs
        assert call_kwargs["max_tokens"] >= 64


# ─── WorkingMemory.try_start_compression (concurrency guard) ─


class TestCompressionGuard:
    async def test_try_start_compression_spawns_task(self):
        wm = WorkingMemory(max_tokens=100)
        wm.add_section(_make_section(name="a", token_count=200, compressible=True))
        client = _make_llm_client(summary="short")
        wm.try_start_compression(client)
        assert wm._compression_task is not None
        # Wait for task to finish
        await wm._compression_task

    async def test_second_try_is_noop_while_running(self):
        """Concurrent calls to try_start_compression should not spawn duplicate tasks."""
        wm = WorkingMemory(max_tokens=100)
        wm.add_section(
            _make_section(name="a", token_count=200, content="a" * 800, compressible=True)
        )

        # Use an event to control when compression completes
        gate = asyncio.Event()
        original_compress = wm.compress_if_needed

        async def slow_compress(llm_client: LLMClient) -> None:
            await gate.wait()
            await original_compress(llm_client)

        wm.compress_if_needed = slow_compress  # type: ignore[assignment]

        client = _make_llm_client(summary="short")
        wm.try_start_compression(client)
        first_task = wm._compression_task

        # Second call while first is still running
        wm.try_start_compression(client)
        assert wm._compression_task is first_task  # Same task, not a new one

        # Release and wait
        gate.set()
        await first_task

    async def test_new_compression_after_previous_done(self):
        wm = WorkingMemory(max_tokens=100)
        wm.add_section(_make_section(name="a", token_count=200, compressible=True))
        client = _make_llm_client(summary="short")

        wm.try_start_compression(client)
        await wm._compression_task
        first_task = wm._compression_task

        # Add more content to go over budget again
        wm.add_section(_make_section(name="b", token_count=200, compressible=True))
        wm.try_start_compression(client)
        assert wm._compression_task is not first_task


# ─── WorkingMemory.close ────────────────────────────────────


class TestClose:
    async def test_close_awaits_compression(self):
        wm = WorkingMemory(max_tokens=100)
        wm.add_section(_make_section(name="a", token_count=200, compressible=True))
        client = _make_llm_client(summary="short")
        wm.try_start_compression(client)
        await wm.close()
        assert wm.total_tokens() == 0  # sections cleared

    async def test_close_without_compression(self):
        wm = WorkingMemory()
        wm.add_section(_make_section(name="a", token_count=50))
        await wm.close()
        assert wm.total_tokens() == 0

    async def test_close_handles_failed_compression(self):
        wm = WorkingMemory(max_tokens=100)
        wm.add_section(_make_section(name="a", token_count=200, compressible=True))
        client = _make_llm_client()
        client._provider.create_message = AsyncMock(side_effect=RuntimeError("boom"))
        wm.try_start_compression(client)
        # Should not raise
        await wm.close()


# ─── PR #54 review: compression size guard ──────────────────


class TestCompressionSizeGuard:
    """Verify compress_if_needed() skips replacement when summary is not smaller.

    PR #54 review Should-Fix #1: a summary as long or longer than the
    original would not reduce total tokens and could loop.
    """

    async def test_larger_summary_skipped(self):
        """When LLM produces a longer summary, the original section is kept."""
        wm = WorkingMemory(max_tokens=100)
        wm.add_section(_make_section(name="a", content="short", token_count=200, compressible=True))

        # Return a summary that is much longer than the original content
        long_summary = "x" * 2000  # ~500 tokens via chars/4
        client = _make_llm_client(summary=long_summary)
        await wm.compress_if_needed(client)

        # Section should still exist with original token count (not replaced)
        section = wm.get_section("a")
        assert section is not None
        assert section.token_count == 200
        assert section.content == "short"

    async def test_equal_size_summary_skipped(self):
        """Summary with same token count as original is also skipped."""
        wm = WorkingMemory(max_tokens=100)
        # 80 chars / 4 = 20 tokens
        original = "a" * 80
        wm.add_section(_make_section(name="a", content=original, token_count=20, compressible=True))

        summary = "b" * 80  # still 20 tokens — not smaller
        client = _make_llm_client(summary=summary)
        await wm.compress_if_needed(client)

        section = wm.get_section("a")
        assert section is not None
        assert section.content == original

    async def test_smaller_summary_accepted(self):
        """Summary that is genuinely smaller gets applied."""
        wm = WorkingMemory(max_tokens=100)
        wm.add_section(
            _make_section(name="a", content="a" * 800, token_count=200, compressible=True)
        )

        client = _make_llm_client(summary="tiny")  # ~1 token
        await wm.compress_if_needed(client)

        section = wm.get_section("a")
        assert section is not None
        assert section.content == "tiny"
        assert section.token_count < 200
