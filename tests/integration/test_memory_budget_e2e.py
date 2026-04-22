"""Integration tests for the MemoryBudget allocate-loop (RFC 0017 PR 4).

Drives a persona agent through a synthetic event stream and asserts:
  (a) working_memory total tokens stay ≤ _MEMORY_BUDGET_TOKENS at every step
  (b) low-signal events (TICK, "hi") admit ~0 memory tokens
  (c) substantive events admit non-zero memory tokens ≤ _MEMORY_BUDGET_TOKENS

Uses an in-memory SQLite DB seeded with a realistic memory snapshot and the
same mock-LLM + _MemoryContextMixin wiring as the persona e2e tests.

This test file is designed to be reusable as a fixture source for PR 5's
empty-context TICK short-circuit tests.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.memory.episodic import (
    DEFAULT_EPISODIC_MIN_SCORE,
    DEFAULT_NOTES_MIN_SCORE,
    EpisodicMemory,
)
from agents.memory.working import WorkingMemory
from agents.persona_runtime.memory_context import (
    _MEMORY_BUDGET_TOKENS,
    _MemoryContextMixin,
)
from agents.persona_types import EventType


# ─── Shared fixture: in-memory DB seeded with realistic snapshot ──────────────


@pytest.fixture
async def seeded_episodic() -> AsyncGenerator[EpisodicMemory, None]:
    """EpisodicMemory backed by in-process SQLite, seeded with representative data.

    Seeded with:
    - 5 episodes covering distinct topics (astronomy, cooking, planning, code review,
      and travel) at varying importance levels.
    - 3 notes covering factual knowledge the agent might recall.

    The fixture is yielded open; the test is responsible for not closing it early.
    Cleanup runs after the test function returns.
    """
    mem = EpisodicMemory("test-agent-budget-e2e", db_path=":memory:")
    await mem.initialize()

    # Seed episodes — varied topics so FTS5 query selectivity is meaningful.
    await mem.store_episode(
        "Discussed telescope optics and aperture tradeoffs for astrophotography.",
        context={"event": "chat"},
        importance=0.9,
        tags=["astronomy", "optics"],
    )
    await mem.store_episode(
        "User asked about sourdough starter hydration ratios and fermentation timing.",
        context={"event": "chat"},
        importance=0.7,
        tags=["cooking", "sourdough"],
    )
    await mem.store_episode(
        "Reviewed Q3 project roadmap and identified three milestone dependencies.",
        context={"event": "chat"},
        importance=0.8,
        tags=["planning", "roadmap"],
    )
    await mem.store_episode(
        "Performed Python code review: flagged missing type hints in api/routes.py.",
        context={"event": "chat"},
        importance=0.6,
        tags=["code-review", "python"],
    )
    await mem.store_episode(
        "User mentioned upcoming trip to Kyoto; discussed cherry blossom season dates.",
        context={"event": "chat"},
        importance=0.5,
        tags=["travel", "japan"],
    )

    # Seed notes — structured knowledge the agent stored.
    await mem.store_note(
        topic="astronomy",
        content=(
            "Aperture determines light gathering for telescope optics and astrophotography. "
            "Larger aperture → fainter objects visible. Rule of thumb: "
            "double the aperture → 4× the light-gathering area."
        ),
        tags=["optics"],
    )
    await mem.store_note(
        topic="sourdough",
        content=(
            "100% hydration starter: equal parts flour and water by weight. "
            "Active starter ready when it doubles within 4–8 hours at 24°C."
        ),
        tags=["baking"],
    )
    await mem.store_note(
        topic="project-planning",
        content=(
            "Q3 milestones: M1 = RFC freeze (week 6), M2 = beta release (week 10), "
            "M3 = GA (week 14). M2 depends on M1 + integration test suite green."
        ),
        tags=["planning"],
    )

    yield mem
    await mem.close()

# The standard query string produced by _LLMPersonaAgent._format_event for TICK events.
# RFC 0017 PR 4: TICK events now call recall() with this query; the min_score threshold
# filters low-signal results so TICK against an unrelated seeded DB admits 0 tokens.
_TICK_QUERY = "Autonomous tick: review your goals and decide on next actions."


@dataclass
class _FakeRelSummary:
    other_participant_id: str
    other_participant_type: str = "user"
    interaction_count: int = 3
    trust_score: float = 0.7
    notes: str = "Prefers concise answers; domain: software engineering."


class _ConcreteMemoryMixin(_MemoryContextMixin):
    def _format_event(self, event: Any) -> str:  # type: ignore[override]
        # Mirror the real _LLMPersonaAgent._format_event for TICK events so
        # integration tests exercise the actual query path.
        if event.event_type == EventType.TICK:
            return _TICK_QUERY
        payload = getattr(event, "payload", {}) or {}
        return str(payload.get("content", ""))


def _make_mixin_with_real_episodic(
    episodic: EpisodicMemory,
    *,
    rel: _FakeRelSummary | None = None,
    sender_id: str | None = None,
    event_type: str = "MESSAGE_RECEIVED",
    content: str = "hello",
) -> tuple[_ConcreteMemoryMixin, Any]:
    """Wire a real EpisodicMemory into the mixin; mock relationship memory."""
    mixin = _ConcreteMemoryMixin()
    mixin.agent_id = "test-agent-budget-e2e"
    mixin._working_memory = WorkingMemory(max_tokens=8192)
    mixin._episodic_memory = episodic

    mixin._relationship_memory = AsyncMock()
    mixin._relationship_memory.get_relationship_summary.return_value = rel

    et = getattr(EventType, event_type)
    event = MagicMock()
    event.event_type = et
    event.sender_id = sender_id
    event.metadata = {}
    event.payload = {"content": content}

    return mixin, event


# ─── Test: four-event stream ───────────────────────────────────────────────────


class TestMemoryBudgetE2EFourEventStream:
    """RFC 0017 PR 4 acceptance: 4-event stream through the wired allocate-loop.

    Event sequence:
      1. TICK          — autonomous heartbeat; expect ~0 admitted tokens
      2. MESSAGE_RECEIVED("hi")  — low-signal greeting; expect ~0 admitted tokens
      3. MESSAGE_RECEIVED(substantive query with keywords from seeded episodes)
                       — high-signal; expect non-zero admitted tokens ≤ budget
      4. TICK          — autonomous heartbeat again; expect ~0 admitted tokens

    The seeded episodic DB uses FTS5 with min_score=0.20 to filter low-signal
    matches.  The TICK and "hi" queries have no lexical overlap with seeded
    topics and should return empty result sets.
    """

    @pytest.mark.asyncio
    async def test_budget_ceiling_holds_at_every_step(
        self, seeded_episodic: EpisodicMemory,
    ) -> None:
        """Token budget ≤ _MEMORY_BUDGET_TOKENS at every event step."""
        events_and_types = [
            ("TICK", ""),
            ("MESSAGE_RECEIVED", "hi"),
            ("MESSAGE_RECEIVED", "telescope aperture astrophotography optics"),
            ("TICK", ""),
        ]
        for et, content in events_and_types:
            mixin, event = _make_mixin_with_real_episodic(
                seeded_episodic, event_type=et, content=content,
            )
            result = await mixin._inject_memory_context(event)
            assert result.memory_admitted_tokens <= _MEMORY_BUDGET_TOKENS, (
                f"Event ({et!r}, {content[:30]!r}) admitted "
                f"{result.memory_admitted_tokens} tokens, "
                f"budget is {_MEMORY_BUDGET_TOKENS}"
            )

    @pytest.mark.asyncio
    async def test_low_signal_events_admit_zero_tokens(
        self, seeded_episodic: EpisodicMemory,
    ) -> None:
        """TICK and low-keyword 'hi' admit 0 memory tokens.

        Real FTS5 DB with min_score=0.20: the standard TICK query
        ("Autonomous tick: review your goals...") and "hi" both produce no
        FTS5 matches above the threshold against the seeded astronomy/cooking/
        planning/code/travel episodes, so the allocate-loop admits nothing.
        """
        # PR #148 review M-1: the zero-admission assertion depends on FTS5
        # BM25 scoring being available.  On SQLite builds without FTS5,
        # ``recall()`` falls through to the LIKE path which (per RFC 0017
        # §C) scores every match at 1.0 and ignores ``min_score`` entirely
        # — a single keyword overlap (e.g. "review" appearing in both the
        # TICK query and the "planning" episode summary) would then admit
        # tokens and break the assertion.  Skip cleanly in LIKE-fallback
        # environments rather than emit a flaky failure.
        if not seeded_episodic.has_fts5:
            pytest.skip(
                "Requires SQLite FTS5: LIKE fallback ignores min_score "
                "(RFC 0017 §C) so zero-admission cannot be guaranteed.",
            )
        for et, content in [("TICK", ""), ("MESSAGE_RECEIVED", "hi")]:
            mixin, event = _make_mixin_with_real_episodic(
                seeded_episodic, event_type=et, content=content,
            )
            result = await mixin._inject_memory_context(event)
            assert result.memory_admitted_tokens == 0, (
                f"Low-signal ({et!r}, {content!r}) admitted "
                f"{result.memory_admitted_tokens} tokens; expected 0. "
                f"Check that min_score={DEFAULT_EPISODIC_MIN_SCORE} filters "
                f"low-signal FTS5 results."
            )

    @pytest.mark.asyncio
    async def test_substantive_event_admits_nonzero_tokens(
        self, seeded_episodic: EpisodicMemory,
    ) -> None:
        """Substantive keyword query → non-zero tokens admitted.

        Uses a keyword-focused query (not a full natural language sentence)
        so FTS5 term matching against the seeded episode summaries is reliable.
        """
        mixin, event = _make_mixin_with_real_episodic(
            seeded_episodic,
            event_type="MESSAGE_RECEIVED",
            content="telescope aperture astrophotography optics",
        )
        result = await mixin._inject_memory_context(event)

        assert result.memory_admitted_tokens > 0, (
            "Substantive keyword query produced 0 admitted tokens; "
            "check FTS5 indexing and min_score calibration."
        )
        assert result.memory_admitted_tokens <= _MEMORY_BUDGET_TOKENS

    @pytest.mark.asyncio
    async def test_substantive_event_populates_memory_sections(
        self, seeded_episodic: EpisodicMemory,
    ) -> None:
        """Substantive keyword query → at least one memory section populated.

        With the 5-episode corpus, BM25 IDF may score the astronomy episode
        below the 0.20 threshold (more documents in the corpus reduces IDF
        for shared terms like 'telescope' and 'aperture').  The astronomy
        note, stored in a separate FTS5 table with a smaller corpus, reliably
        scores above the threshold.  The assertion accepts either
        ``episodic_recall`` or ``recent_notes`` so the test remains correct
        regardless of corpus-size-driven IDF fluctuations.
        """
        mixin, event = _make_mixin_with_real_episodic(
            seeded_episodic,
            event_type="MESSAGE_RECEIVED",
            content="telescope aperture astrophotography optics",
        )
        await mixin._inject_memory_context(event)

        section_names = [s.name for s in mixin._working_memory._sections]
        memory_sections = {"episodic_recall", "recent_notes"}
        assert memory_sections & set(section_names), (
            f"Expected at least one memory section from {memory_sections}, "
            f"got {section_names}"
        )

    @pytest.mark.asyncio
    async def test_tick_admits_zero_tokens_with_real_query(
        self, seeded_episodic: EpisodicMemory,
    ) -> None:
        """TICK with real query string admits 0 tokens (TICK skip removed in PR 4).

        The standard TICK query ("Autonomous tick: review your goals...") does not
        match the seeded astronomy/cooking/planning episodes above the 0.20 threshold,
        so the allocate-loop admits 0 tokens.  This contrasts with the pre-PR-4 state
        where the TICK skip would have returned before even calling recall().

        The behaviour is the same (0 tokens admitted) but the path is different:
        - Pre-PR-4: TICK skip short-circuits before recall()
        - Post-PR-4: recall() runs, DB returns 0 results, allocate-loop admits 0
        The unit test `test_tick_calls_episodic_recall` verifies the path difference.
        """
        # PR #148 review M-1: see ``test_low_signal_events_admit_zero_tokens``
        # above for the LIKE-fallback rationale.  The TICK query
        # contains the token "review", which appears in the seeded
        # "planning" episode summary; under LIKE fallback that match
        # would score 1.0 and bypass ``min_score``.
        if not seeded_episodic.has_fts5:
            pytest.skip(
                "Requires SQLite FTS5: LIKE fallback ignores min_score "
                "(RFC 0017 §C) so zero-admission cannot be guaranteed.",
            )
        mixin, event = _make_mixin_with_real_episodic(
            seeded_episodic,
            event_type="TICK",
            content="",  # _format_event returns _TICK_QUERY for TICK events
        )
        result = await mixin._inject_memory_context(event)

        assert result.memory_admitted_tokens == 0, (
            f"TICK admitted {result.memory_admitted_tokens} tokens; expected 0. "
            f"The standard TICK query should not match seeded topics at "
            f"min_score={DEFAULT_EPISODIC_MIN_SCORE}."
        )

    @pytest.mark.asyncio
    async def test_no_recency_fallback_for_low_signal_message(
        self, seeded_episodic: EpisodicMemory,
    ) -> None:
        """Low-signal MESSAGE_RECEIVED: no recency fallback; memory sections absent.

        The should_fall_back path (PR 2) triggered a recall_notes("", limit=3)
        fallback for empty-notes + empty-episodes + MESSAGE_RECEIVED.  That
        fallback is deleted in PR 4; low-signal messages must leave all sections
        absent rather than injecting unrelated recent notes.
        """
        mixin, event = _make_mixin_with_real_episodic(
            seeded_episodic,
            event_type="MESSAGE_RECEIVED",
            content="hi",
        )
        await mixin._inject_memory_context(event)

        section_names = [s.name for s in mixin._working_memory._sections]
        assert "episodic_recall" not in section_names
        assert "recent_notes" not in section_names


# ─── Test: min_score wiring ────────────────────────────────────────────────────


class TestMinScoreWiredIntoRecallCalls:
    """PR 4: DEFAULT_*_MIN_SCORE constants are passed to recall() calls."""

    @pytest.mark.asyncio
    async def test_episodic_recall_receives_min_score_kwarg(self) -> None:
        """recall() is called with min_score=DEFAULT_EPISODIC_MIN_SCORE."""
        mixin = _ConcreteMemoryMixin()
        mixin.agent_id = "wire-test"
        mixin._working_memory = WorkingMemory(max_tokens=8192)
        mixin._episodic_memory = AsyncMock()
        mixin._episodic_memory.recall.return_value = []
        mixin._episodic_memory.recall_notes.return_value = []
        mixin._relationship_memory = AsyncMock()
        mixin._relationship_memory.get_relationship_summary.return_value = None

        event = MagicMock()
        event.event_type = EventType.MESSAGE_RECEIVED
        event.sender_id = None
        event.metadata = {}
        event.payload = {"content": "some query"}

        await mixin._inject_memory_context(event)

        call_kwargs = mixin._episodic_memory.recall.call_args
        assert call_kwargs.kwargs.get("min_score") == DEFAULT_EPISODIC_MIN_SCORE, (
            f"recall() min_score kwarg is "
            f"{call_kwargs.kwargs.get('min_score')!r}, "
            f"expected {DEFAULT_EPISODIC_MIN_SCORE}"
        )

    @pytest.mark.asyncio
    async def test_recall_notes_receives_min_score_kwarg(self) -> None:
        """recall_notes() is called with min_score=DEFAULT_NOTES_MIN_SCORE."""
        mixin = _ConcreteMemoryMixin()
        mixin.agent_id = "wire-test"
        mixin._working_memory = WorkingMemory(max_tokens=8192)
        mixin._episodic_memory = AsyncMock()
        mixin._episodic_memory.recall.return_value = []
        mixin._episodic_memory.recall_notes.return_value = []
        mixin._relationship_memory = AsyncMock()
        mixin._relationship_memory.get_relationship_summary.return_value = None

        event = MagicMock()
        event.event_type = EventType.MESSAGE_RECEIVED
        event.sender_id = None
        event.metadata = {}
        event.payload = {"content": "some query"}

        await mixin._inject_memory_context(event)

        call_kwargs = mixin._episodic_memory.recall_notes.call_args
        assert call_kwargs.kwargs.get("min_score") == DEFAULT_NOTES_MIN_SCORE, (
            f"recall_notes() min_score kwarg is "
            f"{call_kwargs.kwargs.get('min_score')!r}, "
            f"expected {DEFAULT_NOTES_MIN_SCORE}"
        )

    @pytest.mark.asyncio
    async def test_tick_event_episodic_recall_receives_min_score(self) -> None:
        """TICK event: recall() still called with min_score (no TICK skip in PR 4)."""
        mixin = _ConcreteMemoryMixin()
        mixin.agent_id = "wire-test"
        mixin._working_memory = WorkingMemory(max_tokens=8192)
        mixin._episodic_memory = AsyncMock()
        mixin._episodic_memory.recall.return_value = []
        mixin._episodic_memory.recall_notes.return_value = []
        mixin._relationship_memory = AsyncMock()
        mixin._relationship_memory.get_relationship_summary.return_value = None

        event = MagicMock()
        event.event_type = EventType.TICK
        event.sender_id = None
        event.metadata = {}
        event.payload = {"content": ""}

        await mixin._inject_memory_context(event)

        # TICK skip removed: recall() must have been called.
        mixin._episodic_memory.recall.assert_called_once()
        call_kwargs = mixin._episodic_memory.recall.call_args
        assert call_kwargs.kwargs.get("min_score") == DEFAULT_EPISODIC_MIN_SCORE
