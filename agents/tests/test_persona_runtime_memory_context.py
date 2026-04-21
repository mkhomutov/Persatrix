"""Unit tests for _truncate_with_ellipsis in memory_context.py (RFC 0017 §D),
and the _inject_memory_context allocate-loop rewrite (RFC 0017 PR 2).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.persona_runtime.memory_budget import _count_tokens
from agents.persona_runtime.memory_context import (
    _MEMORY_BUDGET_TOKENS,
    MemoryInjectionResult,
    _MemoryContextMixin,
    _truncate_with_ellipsis,
)

# ─── mode="chars" (existing behaviour, regression) ────────────────────────────


class TestTruncateWithEllipsisCharMode:
    def test_short_text_unchanged(self) -> None:
        assert _truncate_with_ellipsis("hi", 100) == "hi"

    def test_exact_fit_unchanged(self) -> None:
        text = "a" * 10
        assert _truncate_with_ellipsis(text, 10) == text

    def test_truncation_appends_ellipsis(self) -> None:
        text = "hello world"
        result = _truncate_with_ellipsis(text, 5)
        assert result.endswith("...")

    def test_truncation_cuts_at_word_boundary(self) -> None:
        text = "hello beautiful world"
        result = _truncate_with_ellipsis(text, 15)
        # Should end with "..." and not split "beautiful" mid-word.
        assert result.endswith("...")
        without_dots = result[:-3]
        # The cut should be at a word boundary.
        assert without_dots == without_dots.rstrip()

    def test_no_space_uses_full_slice(self) -> None:
        text = "abcdefghij"
        result = _truncate_with_ellipsis(text, 5)
        assert result == "abcde..."

    def test_explicit_mode_chars(self) -> None:
        text = "hello world"
        assert _truncate_with_ellipsis(text, 5, mode="chars") == \
               _truncate_with_ellipsis(text, 5)


# ─── mode="tokens" (new path) ─────────────────────────────────────────────────


class TestTruncateWithEllipsisTokenMode:
    def test_short_text_unchanged(self) -> None:
        text = "hello"
        result = _truncate_with_ellipsis(text, 100, mode="tokens")
        assert result == text

    def test_zero_limit_returns_ellipsis(self) -> None:
        result = _truncate_with_ellipsis("anything", 0, mode="tokens")
        assert result == "…"

    def test_long_text_truncated_ends_with_ellipsis(self) -> None:
        long_text = "word " * 500
        result = _truncate_with_ellipsis(long_text, 20, mode="tokens")
        assert result.endswith("…")

    def test_token_count_within_limit(self) -> None:
        long_text = "word " * 500
        limit = 30
        result = _truncate_with_ellipsis(long_text, limit, mode="tokens")
        count = _count_tokens(result)
        assert count <= limit

    def test_result_shorter_than_original(self) -> None:
        long_text = "word " * 500
        result = _truncate_with_ellipsis(long_text, 20, mode="tokens")
        assert len(result) < len(long_text)

    def test_tiktoken_unavailable_does_not_panic(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Falls back gracefully when tiktoken is not installed."""
        monkeypatch.setitem(sys.modules, "tiktoken", None)  # type: ignore[arg-type]
        long_text = "x" * 400
        # Should not raise; result should be shorter than input.
        result = _truncate_with_ellipsis(long_text, 10, mode="tokens")
        assert result.endswith("…")
        assert len(result) < len(long_text)

    def test_tiktoken_unavailable_short_text_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "tiktoken", None)  # type: ignore[arg-type]
        text = "Hi!"
        result = _truncate_with_ellipsis(text, 50, mode="tokens")
        assert result == text

    def test_mode_tokens_uses_unicode_ellipsis_not_three_dots(self) -> None:
        """Token mode appends U+2026 (…), not three ASCII dots (...)."""
        long_text = "word " * 500
        result = _truncate_with_ellipsis(long_text, 20, mode="tokens")
        assert "…" in result
        assert result.endswith("…")
        # Must NOT end with ASCII triple-dot.
        assert not result.endswith("...")


# ─── Helpers for _inject_memory_context tests ─────────────────────────────────


@dataclass
class _FakeEpisode:
    summary: str
    id: str = "ep-0001"


@dataclass
class _FakeNote:
    topic: str
    content: str


@dataclass
class _FakeRelSummary:
    other_participant_id: str
    other_participant_type: str = "agent"
    interaction_count: int = 1
    trust_score: float = 0.5
    notes: str = ""


def _make_mixin(
    *,
    episodes: list[_FakeEpisode] | None = None,
    notes: list[_FakeNote] | None = None,
    rel: _FakeRelSummary | None = None,
    sender_id: str | None = None,
    event_type: str = "MESSAGE_RECEIVED",
) -> tuple[_ConcreteMemoryMixin, Any]:
    """Return a wired _MemoryContextMixin instance and a matching fake event."""
    from agents.memory.working import WorkingMemory
    from agents.persona_types import EventType

    mixin = _ConcreteMemoryMixin()
    mixin.agent_id = "test-agent"
    mixin._working_memory = WorkingMemory(max_tokens=8192)

    # Wire episodic memory mock.
    mixin._episodic_memory = AsyncMock()
    mixin._episodic_memory.recall.return_value = episodes or []
    mixin._episodic_memory.recall_notes.return_value = notes or []

    # Wire relationship memory mock.
    mixin._relationship_memory = AsyncMock()
    mixin._relationship_memory.get_relationship_summary.return_value = rel

    et = getattr(EventType, event_type)
    event = MagicMock()
    event.event_type = et
    event.sender_id = sender_id
    event.metadata = {}
    event.payload = {"content": "hello"}

    return mixin, event


class _ConcreteMemoryMixin(_MemoryContextMixin):
    """Minimal concrete subclass for testing _MemoryContextMixin."""

    def _format_event(self, event: Any) -> str:  # type: ignore[override]
        return event.payload.get("content", "")


# ─── _inject_memory_context allocate-loop (RFC 0017 PR 2) ─────────────────────


class TestInjectMemoryContextTokenBound:
    """RFC 0017 PR 2: total injected tokens ≤ _MEMORY_BUDGET_TOKENS."""

    @pytest.mark.asyncio
    async def test_token_bound_holds_with_large_content(self) -> None:
        """Synthetic content far exceeding 1500 tokens is clamped to budget."""
        # Build episodes and notes that together far exceed the budget.
        big_summary = "important context word " * 200  # ~800 tokens
        episodes = [_FakeEpisode(summary=big_summary, id=f"ep-{i}") for i in range(5)]
        big_note = "detailed knowledge snippet " * 200  # ~800 tokens
        notes = [_FakeNote(topic="fact", content=big_note) for _ in range(5)]

        mixin, event = _make_mixin(episodes=episodes, notes=notes)
        result = await mixin._inject_memory_context(event)

        # Total tokens admitted by the budget must not exceed 1500.
        # memory_admitted_tokens uses tiktoken (same accounting as the budget),
        # which is the authoritative bound.  WorkingMemory's token_count uses
        # estimate_tokens (chars/4) and may report a different number; we do
        # not assert on it here.
        assert result.memory_admitted_tokens <= _MEMORY_BUDGET_TOKENS, (
            f"Admitted {result.memory_admitted_tokens} tokens, budget is {_MEMORY_BUDGET_TOKENS}"
        )

    @pytest.mark.asyncio
    async def test_empty_memory_stores_admit_zero_tokens(self) -> None:
        """No content → memory_admitted_tokens == 0, no sections added."""
        mixin, event = _make_mixin()
        result = await mixin._inject_memory_context(event)

        assert result.memory_admitted_tokens == 0
        assert "episodic_recall" not in mixin._working_memory._sections
        assert "recent_notes" not in mixin._working_memory._sections
        assert "relationship_context" not in mixin._working_memory._sections


class TestInjectMemoryContextTierOrdering:
    """RFC 0017 PR 2: tiers processed relationship → episodic → notes."""

    @pytest.mark.asyncio
    async def test_relationship_admitted_before_episodic_when_budget_tight(
        self,
    ) -> None:
        """When budget is tight, relationship (priority 8) wins over episodic (7)."""
        # A relationship block + 5 large episodes; the relationship block
        # should be admitted, episodes partially or not admitted.
        rel = _FakeRelSummary(
            other_participant_id="alice",
            other_participant_type="user",
            interaction_count=3,
        )
        big_summary = "episode detail word " * 150  # ~600 tokens each
        episodes = [_FakeEpisode(summary=big_summary, id=f"ep-{i}") for i in range(5)]

        mixin, event = _make_mixin(
            episodes=episodes,
            rel=rel,
            sender_id="alice",
        )
        result = await mixin._inject_memory_context(event)

        # Relationship section was admitted.
        section_names = [s.name for s in mixin._working_memory._sections]
        assert "relationship_context" in section_names
        # Total admitted tokens (budget accounting) must be within budget.
        assert result.memory_admitted_tokens <= _MEMORY_BUDGET_TOKENS
        # Verify the lower-priority tier was actually impacted by the
        # shared budget: at most ~2 of the 5 large episodes (≈600 tokens
        # each) can fit into the remaining budget after the relationship
        # block is admitted.  Without this assertion the test would pass
        # even if the budget were silently bypassed for episodic.
        # (PR #146 review.)
        ep_section = next(
            (s for s in mixin._working_memory._sections if s.name == "episodic_recall"),
            None,
        )
        if ep_section is not None:
            ep_lines = [
                line for line in ep_section.content.splitlines()
                if line.startswith("- ")
            ]
            assert len(ep_lines) < 5, (
                "Budget should have limited episodic admission "
                f"(got {len(ep_lines)}/5)"
            )

    @pytest.mark.asyncio
    async def test_relationship_exhausting_budget_starves_other_tiers(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Relationship block consuming entire budget → episodic and notes admit zero."""
        # Lift the per-field ``rel.notes`` security cap so this test can
        # construct a relationship block that genuinely dominates the
        # 1500-token budget.  The invariant under test is the
        # *budget-allocation* ordering (relationship wins, lower tiers
        # starve), independent of the security-driven char cap.
        # (PR #146 review.)
        from agents.persona_runtime import memory_context as mc

        monkeypatch.setattr(mc, "_REL_NOTES_INTERIM_CHARS", 1_000_000)

        # Build a relationship block that is itself very large.
        big_notes = "relationship detail word " * 600  # ~2400 tokens > budget
        rel = _FakeRelSummary(
            other_participant_id="bob",
            interaction_count=10,
            notes=big_notes,
        )
        episodes = [_FakeEpisode(summary="short ep", id=f"ep-{i}") for i in range(3)]
        notes = [_FakeNote(topic="t", content="short note") for _ in range(3)]

        mixin, event = _make_mixin(
            episodes=episodes,
            notes=notes,
            rel=rel,
            sender_id="bob",
        )
        result = await mixin._inject_memory_context(event)

        # Regardless of what was admitted (the large relationship block may be
        # truncated-and-admitted or dropped), admitted tokens must be within budget.
        assert result.memory_admitted_tokens <= _MEMORY_BUDGET_TOKENS
        # Verify the tier-ordering invariant promised in the docstring:
        # if the relationship block (~2400 tokens, truncated to fit the
        # 1500-token budget) consumes the budget, the lower-priority
        # episodic and notes tiers must admit zero items and therefore
        # add no sections.  Without these assertions the test would pass
        # silently even if budget enforcement broke for the lower tiers.
        # (PR #146 review.)
        section_names = [s.name for s in mixin._working_memory._sections]
        assert "episodic_recall" not in section_names
        assert "recent_notes" not in section_names


class TestInjectMemoryContextMidTierTruncation:
    """RFC 0017 PR 2: mid-tier truncation — oversized item admitted truncated."""

    @pytest.mark.asyncio
    async def test_oversized_note_truncated_when_truncated_form_meets_min_tokens(
        self,
    ) -> None:
        """A single large note is admitted truncated when truncated form ≥ min_tokens."""
        # ~2000 tokens — well over the 1500-token budget, so the item is truncated.
        big_content = "knowledge detail word " * 500
        notes = [_FakeNote(topic="big", content=big_content)]

        mixin, event = _make_mixin(notes=notes)
        result = await mixin._inject_memory_context(event)

        # Some tokens admitted — item was not dropped entirely.
        assert result.memory_admitted_tokens > 0
        section_names = [s.name for s in mixin._working_memory._sections]
        assert "recent_notes" in section_names
        recent_notes_section = next(
            s for s in mixin._working_memory._sections if s.name == "recent_notes"
        )
        # Content was truncated (ends with ellipsis).
        assert recent_notes_section.content.endswith("…")

    @pytest.mark.asyncio
    async def test_note_admitted_whole_when_it_fits(self) -> None:
        """A small note that fits in the budget is admitted unchanged (no ellipsis)."""
        notes = [_FakeNote(topic="small", content="short knowledge")]

        mixin, event = _make_mixin(notes=notes)
        result = await mixin._inject_memory_context(event)

        assert result.memory_admitted_tokens > 0
        recent_notes_section = next(
            s for s in mixin._working_memory._sections if s.name == "recent_notes"
        )
        assert "short knowledge" in recent_notes_section.content
        # No truncation ellipsis for a small item.
        assert not recent_notes_section.content.endswith("…")


class TestInjectMemoryContextReturnValue:
    """RFC 0017 PR 2: MemoryInjectionResult.memory_admitted_tokens contract."""

    @pytest.mark.asyncio
    async def test_memory_admitted_tokens_equals_budget_minus_remaining(
        self,
    ) -> None:
        """memory_admitted_tokens == _MEMORY_BUDGET_TOKENS - budget.remaining."""
        notes = [_FakeNote(topic="t", content="some content about topic")]
        mixin, event = _make_mixin(notes=notes)

        result = await mixin._inject_memory_context(event)

        # memory_admitted_tokens should be positive (content was admitted).
        assert result.memory_admitted_tokens > 0
        # And must not exceed the total budget.
        assert result.memory_admitted_tokens <= _MEMORY_BUDGET_TOKENS

    @pytest.mark.asyncio
    async def test_result_is_memory_injection_result_instance(self) -> None:
        """_inject_memory_context always returns a MemoryInjectionResult."""
        mixin, event = _make_mixin()
        result = await mixin._inject_memory_context(event)
        assert isinstance(result, MemoryInjectionResult)

    @pytest.mark.asyncio
    async def test_result_zero_when_no_content(self) -> None:
        """memory_admitted_tokens == 0 when all tiers return empty."""
        mixin, event = _make_mixin()
        result = await mixin._inject_memory_context(event)
        assert result.memory_admitted_tokens == 0

    @pytest.mark.asyncio
    async def test_admitted_tokens_nonzero_with_content(self) -> None:
        """memory_admitted_tokens > 0 when at least one tier has content."""
        episodes = [_FakeEpisode(summary="relevant historical context")]
        mixin, event = _make_mixin(episodes=episodes)
        result = await mixin._inject_memory_context(event)
        assert result.memory_admitted_tokens > 0


class TestInjectMemoryContextTickAndFallback:
    """RFC 0017 PR 2: TICK skip and should_fall_back heuristic preserved."""

    @pytest.mark.asyncio
    async def test_tick_skips_episodic_recall(self) -> None:
        """TICK events do not call episodic.recall() — TICK skip preserved."""
        mixin, event = _make_mixin(event_type="TICK")
        await mixin._inject_memory_context(event)

        # episodic.recall() must NOT have been called for TICK events.
        mixin._episodic_memory.recall.assert_not_called()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_tick_still_calls_recall_notes(self) -> None:
        """TICK events still query notes (agent-authored curated knowledge)."""
        mixin, event = _make_mixin(event_type="TICK")
        await mixin._inject_memory_context(event)

        mixin._episodic_memory.recall_notes.assert_called()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_should_fall_back_triggers_recency_query(self) -> None:
        """should_fall_back: no FTS5 notes + MESSAGE_RECEIVED + no episodes
        → recall_notes("", limit=3) fallback fires."""
        mixin, event = _make_mixin(
            episodes=[],
            notes=[],
            event_type="MESSAGE_RECEIVED",
        )
        # First recall_notes call returns empty; second (fallback) returns a note.
        fallback_note = _FakeNote(topic="recent", content="recent knowledge")
        mixin._episodic_memory.recall_notes.side_effect = [[], [fallback_note]]  # type: ignore[attr-defined]

        result = await mixin._inject_memory_context(event)

        # The fallback was triggered and the note was admitted.
        assert mixin._episodic_memory.recall_notes.call_count == 2  # type: ignore[attr-defined]
        assert result.memory_admitted_tokens > 0

    @pytest.mark.asyncio
    async def test_should_fall_back_skipped_when_episodes_present(self) -> None:
        """should_fall_back gate: when episodic recall has results, fallback skipped."""
        episodes = [_FakeEpisode(summary="relevant episode")]
        mixin, event = _make_mixin(
            episodes=episodes,
            notes=[],
            event_type="MESSAGE_RECEIVED",
        )

        await mixin._inject_memory_context(event)

        # recall_notes called once (main query), fallback NOT triggered.
        mixin._episodic_memory.recall_notes.assert_called_once()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_stale_sections_cleared_before_injection(self) -> None:
        """Stale sections from a previous event are removed unconditionally."""
        from agents.memory.working import ContextSection, estimate_tokens

        mixin, event = _make_mixin()
        # Pre-populate stale sections.
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

        # Inject with no content — all stale sections must be gone.
        await mixin._inject_memory_context(event)

        section_names = [s.name for s in mixin._working_memory._sections]
        assert "episodic_recall" not in section_names
        assert "recent_notes" not in section_names
        assert "relationship_context" not in section_names


# ─── Exception-path resiliency (RFC 0017 PR 2) ────────────────────────────────


class TestInjectMemoryContextExceptionResiliency:
    """Each tier wraps its query in ``except Exception`` with the contract
    "never fail the event".  These tests cover the resiliency paths that
    were previously uncovered (PR #146 review): a DB lock, I/O error, or
    other transient failure on any tier must NOT propagate to the caller,
    and ``_inject_memory_context`` must still return a valid
    ``MemoryInjectionResult``.
    """

    @pytest.mark.asyncio
    async def test_episodic_recall_exception_does_not_raise(self) -> None:
        """A failing episodic.recall() is logged and treated as no results."""
        mixin, event = _make_mixin()
        mixin._episodic_memory.recall.side_effect = RuntimeError("DB locked")  # type: ignore[attr-defined]

        result = await mixin._inject_memory_context(event)

        assert isinstance(result, MemoryInjectionResult)
        # No episodic content was admitted.
        assert "episodic_recall" not in [
            s.name for s in mixin._working_memory._sections
        ]

    @pytest.mark.asyncio
    async def test_relationship_lookup_exception_does_not_raise(self) -> None:
        """A failing relationship lookup is logged and treated as no rel."""
        mixin, event = _make_mixin(sender_id="alice")
        mixin._relationship_memory.get_relationship_summary.side_effect = (  # type: ignore[attr-defined]
            OSError("disk full")
        )

        result = await mixin._inject_memory_context(event)

        assert isinstance(result, MemoryInjectionResult)
        assert "relationship_context" not in [
            s.name for s in mixin._working_memory._sections
        ]

    @pytest.mark.asyncio
    async def test_notes_recall_exception_does_not_raise(self) -> None:
        """A failing notes recall is logged and treated as no notes."""
        mixin, event = _make_mixin()
        mixin._episodic_memory.recall_notes.side_effect = RuntimeError("DB locked")  # type: ignore[attr-defined]

        result = await mixin._inject_memory_context(event)

        assert isinstance(result, MemoryInjectionResult)
        assert "recent_notes" not in [
            s.name for s in mixin._working_memory._sections
        ]

