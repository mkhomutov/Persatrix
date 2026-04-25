"""
Tests for EpisodicMemory — episode auto-summarization (compression):
LLM-driven compression, batch processing, partial failures, agent scoping,
and prompt truncation.
"""

import time
from unittest.mock import AsyncMock

import pytest

from agents.memory.episodic import EpisodicMemory
from agents.llm_client import LLMResponse, StopReason, Usage


def _make_llm_response(text: str) -> LLMResponse:
    """Helper to create a mock LLM response."""
    return LLMResponse(
        text=text,
        tool_calls=[],
        stop_reason=StopReason.END_TURN,
        usage=Usage(input_tokens=10, output_tokens=5),
    )


# ─── Episode auto-summarization ─────────────────────────────


class TestSummarizeOldEpisodes:
    """summarize_old_episodes() selects raw episodes older than threshold
    and replaces their summary via LLM, incrementing compression_level."""

    async def test_summarizes_old_raw_episodes(self, memory: EpisodicMemory):
        """Old episodes with compression_level=0 get summarized."""
        db = memory._ensure_db()
        # Insert an old episode (30 days ago)
        old_time = time.time() - 30 * 86400
        ep_id = await memory.store_episode(
            summary="Original long summary about a debugging session",
            context={"task": "debug"},
            outcome="fixed the bug",
        )
        await db.execute(
            "UPDATE episodes SET created_at = ? WHERE id = ?", (old_time, ep_id)
        )
        await db.commit()

        llm_client = AsyncMock()
        llm_client.create_message = AsyncMock(
            return_value=_make_llm_response("Debugged and fixed a bug")
        )

        count = await memory.summarize_old_episodes(7, llm_client)
        assert count == 1

        ep = await memory.get_episode(ep_id)
        assert ep is not None
        assert ep.summary == "Debugged and fixed a bug"
        assert ep.compression_level == 1
        assert ep.compressed_at is not None

    async def test_skips_recent_episodes(self, memory: EpisodicMemory):
        """Episodes newer than threshold are not summarized."""
        await memory.store_episode(
            summary="Recent episode", context={},
        )

        llm_client = AsyncMock()
        count = await memory.summarize_old_episodes(7, llm_client)
        assert count == 0
        llm_client.create_message.assert_not_called()

    async def test_skips_already_summarized(self, memory: EpisodicMemory):
        """Episodes with compression_level >= 1 are not re-summarized."""
        db = memory._ensure_db()
        old_time = time.time() - 30 * 86400
        ep_id = await memory.store_episode(
            summary="Already summarized", context={},
        )
        await db.execute(
            "UPDATE episodes SET created_at = ?, compression_level = 1, "
            "compressed_at = ? WHERE id = ?",
            (old_time, old_time + 1000, ep_id),
        )
        await db.commit()

        llm_client = AsyncMock()
        count = await memory.summarize_old_episodes(7, llm_client)
        assert count == 0
        llm_client.create_message.assert_not_called()

    async def test_handles_llm_returning_none(self, memory: EpisodicMemory):
        """When LLM returns no text, episode is skipped (not corrupted)."""
        db = memory._ensure_db()
        old_time = time.time() - 30 * 86400
        ep_id = await memory.store_episode(
            summary="Original summary", context={},
        )
        await db.execute(
            "UPDATE episodes SET created_at = ? WHERE id = ?", (old_time, ep_id)
        )
        await db.commit()

        llm_client = AsyncMock()
        llm_client.create_message = AsyncMock(
            return_value=_make_llm_response(None)
        )

        count = await memory.summarize_old_episodes(7, llm_client)
        assert count == 0

        ep = await memory.get_episode(ep_id)
        assert ep.summary == "Original summary"
        assert ep.compression_level == 0

    async def test_handles_llm_exception(self, memory: EpisodicMemory):
        """When LLM raises, episode is skipped and error logged."""
        db = memory._ensure_db()
        old_time = time.time() - 30 * 86400
        ep_id = await memory.store_episode(
            summary="Original summary", context={},
        )
        await db.execute(
            "UPDATE episodes SET created_at = ? WHERE id = ?", (old_time, ep_id)
        )
        await db.commit()

        llm_client = AsyncMock()
        llm_client.create_message = AsyncMock(side_effect=RuntimeError("LLM down"))

        count = await memory.summarize_old_episodes(7, llm_client)
        assert count == 0

        ep = await memory.get_episode(ep_id)
        assert ep.summary == "Original summary"
        assert ep.compression_level == 0

    async def test_compression_level_transition_0_to_1(self, memory: EpisodicMemory):
        """Compression level increments: 0 → 1.

        Note: the 1→2 (distilled) transition is not yet reachable
        because summarize_old_episodes() selects compression_level < 1.
        """
        db = memory._ensure_db()
        old_time = time.time() - 30 * 86400
        ep_id = await memory.store_episode(
            summary="Raw episode", context={"data": "value"},
            outcome="success",
        )
        await db.execute(
            "UPDATE episodes SET created_at = ? WHERE id = ?", (old_time, ep_id)
        )
        await db.commit()

        llm_client = AsyncMock()
        llm_client.create_message = AsyncMock(
            return_value=_make_llm_response("Compressed v1")
        )

        await memory.summarize_old_episodes(7, llm_client)
        ep = await memory.get_episode(ep_id)
        assert ep.compression_level == 1

    async def test_compression_model_forwarded_to_llm(self, memory: EpisodicMemory):
        """The compression_model parameter is passed through to LLM client."""
        db = memory._ensure_db()
        old_time = time.time() - 30 * 86400
        ep_id = await memory.store_episode(
            summary="Episode to compress", context={},
        )
        await db.execute(
            "UPDATE episodes SET created_at = ? WHERE id = ?", (old_time, ep_id)
        )
        await db.commit()

        llm_client = AsyncMock()
        llm_client.create_message = AsyncMock(
            return_value=_make_llm_response("Compressed")
        )

        await memory.summarize_old_episodes(
            7, llm_client, compression_model="custom-model-v2"
        )
        llm_client.create_message.assert_called_once()
        call_kwargs = llm_client.create_message.call_args
        assert call_kwargs.kwargs["model"] == "custom-model-v2"

    async def test_partial_batch_failure(self, memory: EpisodicMemory):
        """In a batch of 3 episodes, if the 2nd LLM call fails,
        the 1st and 3rd are still summarized (count == 2)."""
        db = memory._ensure_db()
        old_time = time.time() - 30 * 86400

        ids = []
        for i in range(3):
            ep_id = await memory.store_episode(
                summary=f"Episode {i}", context={"idx": i},
            )
            await db.execute(
                "UPDATE episodes SET created_at = ? WHERE id = ?", (old_time, ep_id)
            )
            ids.append(ep_id)
        await db.commit()

        llm_client = AsyncMock()
        llm_client.create_message = AsyncMock(
            side_effect=[
                _make_llm_response("Compressed 1"),
                RuntimeError("LLM transient failure"),
                _make_llm_response("Compressed 3"),
            ]
        )

        count = await memory.summarize_old_episodes(7, llm_client)
        assert count == 2

        # 1st and 3rd summarized, 2nd left at level 0
        ep0 = await memory.get_episode(ids[0])
        ep1 = await memory.get_episode(ids[1])
        ep2 = await memory.get_episode(ids[2])
        assert ep0.compression_level == 1
        assert ep1.compression_level == 0
        assert ep2.compression_level == 1

    async def test_agent_scoped_summarization(self, memory_pair):
        """Only the calling agent's episodes are summarized."""
        mem_a, mem_b = memory_pair
        db_a = mem_a._ensure_db()
        db_b = mem_b._ensure_db()
        old_time = time.time() - 30 * 86400

        ep_a = await mem_a.store_episode(summary="Agent A episode", context={})
        ep_b = await mem_b.store_episode(summary="Agent B episode", context={})

        await db_a.execute(
            "UPDATE episodes SET created_at = ? WHERE id = ?", (old_time, ep_a)
        )
        await db_a.commit()
        await db_b.execute(
            "UPDATE episodes SET created_at = ? WHERE id = ?", (old_time, ep_b)
        )
        await db_b.commit()

        llm_client = AsyncMock()
        llm_client.create_message = AsyncMock(
            return_value=_make_llm_response("Summarized A")
        )

        count = await mem_a.summarize_old_episodes(7, llm_client)
        assert count == 1

        # Agent B's episode should be unchanged
        ep = await mem_b.get_episode(ep_b)
        assert ep.compression_level == 0
        assert ep.summary == "Agent B episode"

    async def test_negative_older_than_days_raises(self, memory: EpisodicMemory):
        llm_client = AsyncMock()
        with pytest.raises(ValueError, match="older_than_days must be >= 0"):
            await memory.summarize_old_episodes(-1, llm_client)

    async def test_empty_db_returns_zero(self, memory: EpisodicMemory):
        llm_client = AsyncMock()
        count = await memory.summarize_old_episodes(7, llm_client)
        assert count == 0

    async def test_multiple_old_episodes_summarized(self, memory: EpisodicMemory):
        """Multiple old episodes are all summarized in one call."""
        db = memory._ensure_db()
        old_time = time.time() - 30 * 86400

        ids = []
        for i in range(3):
            ep_id = await memory.store_episode(
                summary=f"Old episode {i}", context={"idx": i},
            )
            await db.execute(
                "UPDATE episodes SET created_at = ? WHERE id = ?", (old_time, ep_id)
            )
            ids.append(ep_id)
        await db.commit()

        call_count = 0

        async def mock_create(**kwargs):
            nonlocal call_count
            call_count += 1
            return _make_llm_response(f"Compressed {call_count}")

        llm_client = AsyncMock()
        llm_client.create_message = AsyncMock(side_effect=mock_create)

        count = await memory.summarize_old_episodes(7, llm_client)
        assert count == 3
        assert call_count == 3

    async def test_batch_size_zero_raises(self, memory: EpisodicMemory):
        """batch_size < 1 raises ValueError."""
        llm_client = AsyncMock()
        with pytest.raises(ValueError, match="batch_size must be >= 1"):
            await memory.summarize_old_episodes(7, llm_client, batch_size=0)

    async def test_batch_size_limits_processing(self, memory: EpisodicMemory):
        """With 5 old episodes and batch_size=2, only 2 are summarized."""
        db = memory._ensure_db()
        old_time = time.time() - 30 * 86400

        ids = []
        for i in range(5):
            ep_id = await memory.store_episode(
                summary=f"Old episode {i}", context={"idx": i},
            )
            await db.execute(
                "UPDATE episodes SET created_at = ? WHERE id = ?", (old_time, ep_id)
            )
            ids.append(ep_id)
        await db.commit()

        llm_client = AsyncMock()
        llm_client.create_message = AsyncMock(
            return_value=_make_llm_response("Compressed")
        )

        count = await memory.summarize_old_episodes(7, llm_client, batch_size=2)
        assert count == 2
        assert llm_client.create_message.call_count == 2

        # 3 episodes remain at compression_level 0
        remaining = 0
        for ep_id in ids:
            ep = await memory.get_episode(ep_id)
            if ep.compression_level == 0:
                remaining += 1
        assert remaining == 3

    async def test_context_truncation_in_prompt(self, memory: EpisodicMemory):
        """Episode context > _MAX_CONTEXT_CHARS is truncated in the prompt."""
        db = memory._ensure_db()
        old_time = time.time() - 30 * 86400

        # Create episode with context larger than the 2000-char limit
        large_context = {"data": "x" * 3000}
        ep_id = await memory.store_episode(
            summary="Episode with large context", context=large_context,
        )
        await db.execute(
            "UPDATE episodes SET created_at = ? WHERE id = ?", (old_time, ep_id)
        )
        await db.commit()

        llm_client = AsyncMock()
        llm_client.create_message = AsyncMock(
            return_value=_make_llm_response("Compressed")
        )

        await memory.summarize_old_episodes(7, llm_client)

        # Verify the prompt sent to the LLM contains the truncation marker
        call_kwargs = llm_client.create_message.call_args
        prompt = call_kwargs.kwargs["messages"][0]["content"]
        assert "... [truncated]" in prompt

    async def test_handles_llm_returning_empty_string(self, memory: EpisodicMemory):
        """When LLM returns empty/whitespace text, episode is skipped."""
        db = memory._ensure_db()
        old_time = time.time() - 30 * 86400
        ep_id = await memory.store_episode(
            summary="Original summary", context={},
        )
        await db.execute(
            "UPDATE episodes SET created_at = ? WHERE id = ?", (old_time, ep_id)
        )
        await db.commit()

        llm_client = AsyncMock()
        llm_client.create_message = AsyncMock(
            return_value=_make_llm_response("   ")
        )

        count = await memory.summarize_old_episodes(7, llm_client)
        assert count == 0

        ep = await memory.get_episode(ep_id)
        assert ep.summary == "Original summary"
        assert ep.compression_level == 0
