"""Unit tests for ``agents.memory.scope_recall.recall_with_scope_filter``.

The helper is the single implementation of recall + Python-side scope/tags
filtering.  Both :class:`agents.memory.facade.MemoryFacade.retrieve_relevant`
and the persona-runtime channel-history tier
(:meth:`agents.persona_runtime.memory_context._MemoryContextMixin._inject_memory_context`)
delegate here, so the contract is pinned in one place rather than forked
across two call sites (RFC 0011 PR 5 follow-up).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock

import pytest

from agents.memory.episodic import EpisodicMemory
from agents.memory.scope_recall import (
    TAG_SCOPE_OVERFETCH_FACTOR,
    recall_with_scope_filter,
)


@pytest.fixture
async def episodic() -> AsyncGenerator[EpisodicMemory, None]:
    """An initialised in-memory ``EpisodicMemory`` for direct-store tests."""
    mem = EpisodicMemory(agent_id="scope-recall-test", db_path=":memory:")
    await mem.initialize()
    try:
        yield mem
    finally:
        await mem.close()


# ─── AND-tag semantics ────────────────────────────────────────


async def test_recall_with_scope_filter_tags_and_semantics(
    episodic: EpisodicMemory,
) -> None:
    """RFC 0011 PR 5 contract: the tags filter is AND, not OR."""
    await episodic.store_episode(
        summary="alpha entry has both tags",
        context={},
        importance=0.9,
        tags=["a", "b"],
    )
    await episodic.store_episode(
        summary="beta entry has only a",
        context={},
        importance=0.9,
        tags=["a"],
    )
    await episodic.store_episode(
        summary="gamma entry has only b",
        context={},
        importance=0.9,
        tags=["b"],
    )

    results = await recall_with_scope_filter(
        episodic, "entry", limit=10, tags=("a", "b"),
    )
    summaries = {ep.summary for ep in results}
    assert "alpha entry has both tags" in summaries
    # AND-semantics — partial-tag matches are excluded.
    assert "beta entry has only a" not in summaries
    assert "gamma entry has only b" not in summaries


async def test_recall_with_scope_filter_empty_tags_is_noop(
    episodic: EpisodicMemory,
) -> None:
    """An empty ``tags`` iterable matches every result."""
    await episodic.store_episode(
        summary="alpha entry", context={}, importance=0.9, tags=["x"],
    )
    await episodic.store_episode(
        summary="beta entry", context={}, importance=0.9, tags=[],
    )

    results = await recall_with_scope_filter(
        episodic, "entry", limit=10, tags=(),
    )
    summaries = {ep.summary for ep in results}
    assert {"alpha entry", "beta entry"}.issubset(summaries)


# ─── scope filter: column vs context-fallback ────────────────


async def test_recall_with_scope_filter_scope_column_filter(
    episodic: EpisodicMemory,
) -> None:
    """Column-level ``scope`` filter excludes off-scope rows."""
    await episodic.store_episode(
        summary="planning room turn 1",
        context={},
        importance=0.9,
        scope="group:planning",
    )
    await episodic.store_episode(
        summary="other room turn 1",
        context={},
        importance=0.9,
        scope="group:other",
    )

    results = await recall_with_scope_filter(
        episodic, "room", limit=10, scope="group:planning",
    )
    summaries = {ep.summary for ep in results}
    assert "planning room turn 1" in summaries
    assert "other room turn 1" not in summaries


async def test_recall_with_scope_filter_falls_back_to_context_scope(
    episodic: EpisodicMemory,
) -> None:
    """Pre-PR-220 rows wrote ``scope`` into ``context`` rather than the column.

    The helper's filter must honour ``context["scope"]`` when the column
    is NULL so legacy rows still match the filter.
    """
    # Column is NULL (not passed to store_episode); scope is in context.
    await episodic.store_episode(
        summary="legacy row",
        context={"scope": "channel:legacy"},
        importance=0.9,
    )
    await episodic.store_episode(
        summary="new row",
        context={},
        importance=0.9,
        scope="channel:legacy",
    )
    await episodic.store_episode(
        summary="off-scope row",
        context={"scope": "channel:other"},
        importance=0.9,
    )

    results = await recall_with_scope_filter(
        episodic, "row", limit=10, scope="channel:legacy",
    )
    summaries = {ep.summary for ep in results}
    assert "legacy row" in summaries
    assert "new row" in summaries
    assert "off-scope row" not in summaries


async def test_recall_with_scope_filter_column_wins_over_context(
    episodic: EpisodicMemory,
) -> None:
    """When the column scope is set, the context fallback is ignored.

    Pins the precedence rule from
    :meth:`MemoryFacade.retrieve_relevant`: ``column`` is authoritative;
    ``context["scope"]`` is only a fallback for legacy NULL-column rows.
    """
    # Column says "planning"; context says "other" — the column wins.
    await episodic.store_episode(
        summary="contradictory row",
        context={"scope": "channel:other"},
        importance=0.9,
        scope="channel:planning",
    )
    results_planning = await recall_with_scope_filter(
        episodic, "row", limit=10, scope="channel:planning",
    )
    results_other = await recall_with_scope_filter(
        episodic, "row", limit=10, scope="channel:other",
    )
    assert any(ep.summary == "contradictory row" for ep in results_planning)
    assert all(ep.summary != "contradictory row" for ep in results_other)


# ─── overfetch contract ──────────────────────────────────────


async def test_recall_with_scope_filter_overfetch_caps_at_limit(
    episodic: EpisodicMemory,
) -> None:
    """With a filter active the helper over-fetches but never exceeds ``limit``."""
    # Seed 25 in-scope rows. With limit=5 and overfetch=3, the helper
    # asks for 15 raw rows; after filtering it must return exactly 5.
    for i in range(25):
        await episodic.store_episode(
            summary=f"planning turn {i}",
            context={},
            importance=0.9,
            scope="group:planning",
        )

    results = await recall_with_scope_filter(
        episodic, "planning", limit=5, scope="group:planning",
    )
    assert len(results) == 5


async def test_recall_with_scope_filter_no_filter_skips_overfetch() -> None:
    """Without a scope/tags filter, recall is called with ``limit`` directly.

    The 3× over-fetch only earns its keep when a Python-side filter can
    drop matches; with no filter every match passes through, so paying
    the 3× cost would just inflate I/O.
    """
    fake_episodic = AsyncMock()
    fake_episodic.recall.return_value = []

    await recall_with_scope_filter(fake_episodic, "q", limit=7)
    fake_episodic.recall.assert_awaited_once()
    # Inspect the kwargs passed to ``recall``.
    call_kwargs = fake_episodic.recall.await_args.kwargs
    assert call_kwargs["limit"] == 7
    # No 3× multiplier — pure recall path.
    assert call_kwargs["limit"] != 7 * TAG_SCOPE_OVERFETCH_FACTOR


async def test_recall_with_scope_filter_with_filter_overfetches() -> None:
    """With a scope or tags filter, recall is asked for ``limit * 3``."""
    fake_episodic = AsyncMock()
    fake_episodic.recall.return_value = []

    await recall_with_scope_filter(
        fake_episodic, "q", limit=10, scope="group:planning",
    )
    call_kwargs = fake_episodic.recall.await_args.kwargs
    assert call_kwargs["limit"] == 10 * TAG_SCOPE_OVERFETCH_FACTOR


async def test_recall_with_scope_filter_returns_episode_dataclass(
    episodic: EpisodicMemory,
) -> None:
    """Helper returns ``Episode`` — facade does the ``MemoryEntry`` mapping."""
    from agents.memory.episodic_queries import Episode

    await episodic.store_episode(
        summary="shape-check",
        context={},
        importance=0.7,
        tags=["x"],
        scope="group:planning",
    )
    results = await recall_with_scope_filter(
        episodic, "shape-check", limit=5, scope="group:planning",
    )
    assert len(results) == 1
    assert isinstance(results[0], Episode)
    assert results[0].summary == "shape-check"
    assert results[0].scope == "group:planning"
