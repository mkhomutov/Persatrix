"""Unit tests for RFC 0017 PR 4 gate-removal changes to _inject_memory_context.

Tests that the TICK skip and should_fall_back recency heuristic are removed,
and that min_score is wired into both recall() and recall_notes() calls.

Extracted from test_inject_memory_context.py when that file exceeded the
500-line code size limit.
"""

from __future__ import annotations

import pytest

from agents.persona_runtime.classification import CLASSIFICATION_PUBLIC
from agents.persona_runtime.memory_context import MemoryInjectionResult

# Doubles shared with test_inject_memory_context.py; this file previously
# kept its own copy, and both copies drifted from production identically.
from agents.tests._memory_context_helpers import (
    FakeEpisode as _FakeEpisode,
)
from agents.tests._memory_context_helpers import (
    episodic_tier,
)
from agents.tests._memory_context_helpers import (
    make_mixin as _make_mixin,
)

# ``episodic_tier`` is autouse; see test_inject_memory_context.py for why the
# re-export is load-bearing rather than cosmetic.
__all__ = ["episodic_tier"]


# ─── RFC 0017 PR 4: TICK skip removed; min_score wired; fallback deleted ──────


class TestInjectMemoryContextTickBehavior:
    """RFC 0017 PR 4: TICK skip removed; recall() now called for all event types."""

    @pytest.mark.asyncio
    async def test_tick_calls_episodic_recall(self) -> None:
        """TICK events now call episodic.recall() — TICK skip removed (PR 4).

        The recall-layer min_score threshold filters low-signal results at the
        DB layer; zero-admission TICK events are handled by PR 5's empty-context
        short-circuit.
        """
        mixin, event = _make_mixin(event_type="TICK")
        await mixin._inject_memory_context(event)

        # episodic.recall() MUST be called for TICK events (no skip).
        mixin._episodic_memory.recall.assert_called_once()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_tick_calls_recall_notes(self) -> None:
        """TICK events call recall_notes (unchanged from PR 2)."""
        mixin, event = _make_mixin(event_type="TICK")
        await mixin._inject_memory_context(event)

        mixin._episodic_memory.recall_notes.assert_called_once()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_tick_with_empty_stores_admits_zero_tokens(self) -> None:
        """TICK event with no matching memory → memory_admitted_tokens == 0.

        Previously the TICK skip short-circuited before any recall; now the
        threshold filters at the DB layer and the allocate-loop handles the
        empty result naturally.
        """
        mixin, event = _make_mixin(event_type="TICK")
        result = await mixin._inject_memory_context(event)

        assert isinstance(result, MemoryInjectionResult)
        assert result.memory_admitted_tokens == 0
        section_names = [s.name for s in mixin._working_memory._sections]
        assert "episodic_recall" not in section_names

    @pytest.mark.asyncio
    async def test_tick_with_high_relevance_episode_admits_tokens(self) -> None:
        """TICK event with a high-relevance episode in the mock → content admitted.

        (The mock returns the episode unconditionally; real DB would filter
        low-signal TICK content via min_score.  This test verifies the path
        from received episodes to admitted tokens is intact for TICK events.)

        The episode is pinned ``public`` deliberately.  A TICK is a
        floor-class event: ``acting_classification_for_event`` resolves it
        to ``None`` unconditionally, which is the RFC 0037 §D rule-(b)
        ``public`` floor, so an ``internal`` row — the fixture default —
        is correctly withheld by the gate and could never reach the prompt.
        ``public`` is the only level a TICK can admit.
        """
        episodes = [
            _FakeEpisode(
                summary="high relevance autonomous goal context",
                protection_level=CLASSIFICATION_PUBLIC,
            ),
        ]
        mixin, event = _make_mixin(episodes=episodes, event_type="TICK")
        result = await mixin._inject_memory_context(event)

        assert result.memory_admitted_tokens > 0
        section_names = [s.name for s in mixin._working_memory._sections]
        assert "episodic_recall" in section_names

    @pytest.mark.asyncio
    async def test_min_score_passed_to_episodic_recall(self) -> None:
        """recall() is called with min_score=DEFAULT_EPISODIC_MIN_SCORE (PR 4)."""
        from agents.memory.episodic import DEFAULT_EPISODIC_MIN_SCORE

        mixin, event = _make_mixin()
        await mixin._inject_memory_context(event)

        call_kwargs = mixin._episodic_memory.recall.call_args  # type: ignore[attr-defined]
        assert call_kwargs.kwargs.get("min_score") == DEFAULT_EPISODIC_MIN_SCORE

    @pytest.mark.asyncio
    async def test_min_score_passed_to_recall_notes(self) -> None:
        """recall_notes() is called with min_score=DEFAULT_NOTES_MIN_SCORE (PR 4)."""
        from agents.memory.episodic import DEFAULT_NOTES_MIN_SCORE

        mixin, event = _make_mixin()
        await mixin._inject_memory_context(event)

        call_kwargs = mixin._episodic_memory.recall_notes.call_args  # type: ignore[attr-defined]
        assert call_kwargs.kwargs.get("min_score") == DEFAULT_NOTES_MIN_SCORE

    @pytest.mark.asyncio
    async def test_no_recency_fallback_when_notes_empty(self) -> None:
        """should_fall_back removed (PR 4): recall_notes called exactly once.

        Previously, empty notes + CHANNEL_MESSAGE + no episodes triggered a
        second recall_notes("", limit=3) fallback.  That path is deleted; the
        min_score threshold is the only filter.
        """
        mixin, event = _make_mixin(
            episodes=[],
            notes=[],
            event_type="CHANNEL_MESSAGE",
        )

        await mixin._inject_memory_context(event)

        # recall_notes called once only (no fallback).
        mixin._episodic_memory.recall_notes.assert_called_once()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_no_recency_fallback_when_episodes_present(self) -> None:
        """Fallback absent even when episodes present (was pre-existing guard)."""
        episodes = [_FakeEpisode(summary="relevant episode")]
        mixin, event = _make_mixin(
            episodes=episodes,
            notes=[],
            event_type="CHANNEL_MESSAGE",
        )

        await mixin._inject_memory_context(event)

        # recall_notes called once (main query), no fallback.
        mixin._episodic_memory.recall_notes.assert_called_once()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_stale_sections_cleared_before_injection(self) -> None:
        """Stale sections from a previous event are removed unconditionally."""
        from agents.memory.working import ContextSection, estimate_tokens

        mixin, event = _make_mixin()
        stale_text = "stale content"
        for name, pri in (
            ("episodic_recall", 7),
            ("recent_notes", 6),
            ("relationship_context", 8),
        ):
            mixin._working_memory.add_section(ContextSection(
                name=name,
                content=stale_text,
                priority=pri,
                token_count=estimate_tokens(stale_text),
                compressible=True,
            ))

        await mixin._inject_memory_context(event)

        section_names = [s.name for s in mixin._working_memory._sections]
        assert "episodic_recall" not in section_names
        assert "recent_notes" not in section_names
        assert "relationship_context" not in section_names
