"""
Episodic memory retention — summarization and pruning of old episodes.

Standalone async functions that operate on an open ``aiosqlite.Connection``
and accept ``agent_id`` as an explicit parameter.  No object state; safe to
call from any async context.  Follows the same pattern as
:mod:`~agents.memory.episodic_queries`.
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING

import aiosqlite

from .episodic_queries import EPISODE_SELECT, row_to_episode

if TYPE_CHECKING:
    from ..llm_client import LLMClient

logger = logging.getLogger(__name__)

# Maximum characters of serialised context to include in the summarization
# prompt.  Prevents arbitrarily large episode context dicts from blowing up
# LLM input size.
_MAX_CONTEXT_CHARS: int = 2000

__all__ = [
    "summarize_old_episodes",
    "delete_old_episodes",
    "_MAX_CONTEXT_CHARS",
]


async def summarize_old_episodes(
    db: aiosqlite.Connection,
    agent_id: str,
    older_than_days: float,
    llm_client: LLMClient,
    *,
    compression_model: str = "claude-haiku-4",
    batch_size: int = 50,
) -> int:
    """Summarize raw episodes older than *older_than_days*.

    Selects up to *batch_size* episodes with ``compression_level < 1``
    whose ``created_at`` is older than the threshold, calls the LLM to
    produce a compressed summary, then updates each episode in place.
    Each successful update is committed immediately so that progress is
    not lost if the process crashes mid-batch.

    Callers that need to process a full backlog should invoke this function
    in a loop until it returns 0.

    Not concurrency-safe.  External callers should ensure only one
    summarization run per agent at a time.

    Returns the number of episodes summarized in this batch.
    """
    if batch_size < 1:
        raise ValueError(f"batch_size must be >= 1, got {batch_size}")
    if older_than_days < 0:
        raise ValueError(f"older_than_days must be >= 0, got {older_than_days}")

    cutoff = time.time() - older_than_days * 86400.0

    # NOTE: compression_level < 1 intentionally limits selection to raw
    # (level-0) episodes.  The 1→2 ("distilled") transition defined in
    # the RFC is not yet reachable through this function.  A separate
    # distill_old_episodes() (or a max_compression_level parameter) is
    # planned for a future PR.
    #
    # LIMIT bounds the batch to avoid unbounded serial LLM calls and
    # memory usage for agents with large unsummarized backlogs.  Callers
    # should loop until this function returns 0.
    async with db.execute(
        f"SELECT {EPISODE_SELECT} FROM episodes "
        "WHERE agent_id = ? AND compression_level < 1 AND created_at < ? "
        "ORDER BY created_at ASC LIMIT ?",
        (agent_id, cutoff, batch_size),
    ) as cursor:
        rows = await cursor.fetchall()

    if not rows:
        return 0

    summarized = 0
    for row in rows:
        episode = row_to_episode(row)
        prompt = (
            f"Summarize the following episode concisely, preserving key facts "
            f"and outcomes.\n\n"
            f"Summary: {episode.summary}\n"
        )
        if episode.outcome:
            prompt += f"Outcome: {episode.outcome}\n"
        if episode.tags:
            prompt += f"Tags: {', '.join(episode.tags)}\n"
        if episode.context:
            ctx_str = json.dumps(episode.context)
            if len(ctx_str) > _MAX_CONTEXT_CHARS:
                ctx_str = ctx_str[:_MAX_CONTEXT_CHARS] + "... [truncated]"
            prompt += f"Context: {ctx_str}\n"

        try:
            response = await llm_client.create_message(
                model=compression_model,
                messages=[{"role": "user", "content": prompt}],
                system=(
                    "You are a concise summarizer. "
                    "Distill the episode into a brief summary."
                ),
                tools=[],
                max_tokens=256,
                temperature=0.2,
            )
            summary = response.text
            if response.usage:
                logger.debug(
                    "Summarization tokens for episode %s: in=%d out=%d",
                    episode.id,
                    response.usage.input_tokens,
                    response.usage.output_tokens,
                )
            if summary is None or not summary.strip():
                logger.warning(
                    "Summarization of episode %s returned %s, skipping",
                    episode.id,
                    "no text" if summary is None else "empty text",
                )
                continue

            # Strip leading/trailing whitespace from LLM output (F-3c-1).
            summary = summary.strip()

            now = time.time()
            new_level = episode.compression_level + 1
            update_cursor = await db.execute(
                "UPDATE episodes SET summary = ?, compression_level = ?, "
                "compressed_at = ? WHERE id = ? AND agent_id = ?",
                (summary, new_level, now, episode.id, agent_id),
            )
            if update_cursor.rowcount > 0:
                # Commit each episode individually so that progress is
                # durable even if the process crashes mid-batch.
                await db.commit()
                summarized += 1
                logger.info(
                    "Summarized episode %s: compression_level %d → %d",
                    episode.id,
                    episode.compression_level,
                    new_level,
                )
        except Exception:
            logger.warning(
                "Failed to summarize episode %s", episode.id, exc_info=True,
            )

    return summarized


async def delete_old_episodes(
    db: aiosqlite.Connection,
    agent_id: str,
    older_than_days: float,
) -> int:
    """Delete compressed episodes older than *older_than_days*.

    Only episodes with ``compression_level >= 1`` are eligible for
    deletion.  Uncompressed (raw) episodes are never deleted — they
    must be summarized first via :func:`summarize_old_episodes`.

    Returns the number of episodes deleted.
    """
    if older_than_days < 0:
        raise ValueError(f"older_than_days must be >= 0, got {older_than_days}")

    cutoff = time.time() - older_than_days * 86400.0

    cursor = await db.execute(
        "DELETE FROM episodes "
        "WHERE agent_id = ? AND compression_level >= 1 AND created_at < ?",
        (agent_id, cutoff),
    )
    deleted = cursor.rowcount
    if deleted:
        await db.commit()
        logger.info(
            "Deleted %d compressed episodes older than %.1f days for agent %s",
            deleted,
            older_than_days,
            agent_id,
        )
    return deleted
