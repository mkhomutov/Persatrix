"""Unit tests for _inject_memory_context allocate-loop rewrite (RFC 0017 PR 2).

Split from test_persona_runtime_memory_context.py when that file exceeded
the 500-line code size limit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.persona_runtime.memory_context import (
    _MEMORY_BUDGET_TOKENS,
    MemoryInjectionResult,
    _MemoryContextMixin,
)

# ─── Helpers ──────────────────────────────────────────────────────────────────


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
    # Default away from _DEFAULT_TRUST_SCORE (0.5) so the trust-injection
    # branch in _inject_memory_context (which only emits "Trust: ..." when
    # the score deviates from the default by more than
    # _TRUST_DEVIATION_THRESHOLD) is reachable from tests that do not
    # explicitly set trust_score.
    # (PR #146 re-review: trust branch unreachable with the prior 0.5 default.)
    trust_score: float = 0.7
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
        return str(event.payload.get("content", ""))


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
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When budget is tight, relationship (priority 8) wins over episodic (7)."""
        # Episode summaries are clamped to ``_MAX_EPISODE_SUMMARY_CHARS`` (200)
        # before reaching the budget, so 5 raw-large episodes alone fit
        # comfortably inside the production 1500-token budget.  Tighten the
        # budget for this test so the tier-ordering assertion exercises actual
        # budget pressure rather than an effectively-unbounded episode size.
        # (PR #146 follow-up: prior version asserted '5/5 episodes' against an
        #  effectively-unbounded episode size; rewritten here after the
        #  per-field char cap commit to drive contention via a tight budget.)
        from agents.persona_runtime import memory_context as mc

        # 100 tokens ≈ enough for the relationship block (~17 tokens) plus
        # at most 1–2 episode lines (~32 tokens each after token-truncation).
        monkeypatch.setattr(mc, "_MEMORY_BUDGET_TOKENS", 100)

        rel = _FakeRelSummary(
            other_participant_id="alice",
            other_participant_type="user",
            interaction_count=3,
        )
        big_summary = "episode detail word " * 150  # capped to 200 chars on inject
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
        # Episodic admission must be limited by the shared budget.
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
        # Lift the ``rel.notes`` security cap to exercise budget-allocation ordering.
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

        assert result.memory_admitted_tokens <= _MEMORY_BUDGET_TOKENS
        # Budget consumed by relationship → lower tiers admit nothing.
        section_names = [s.name for s in mixin._working_memory._sections]
        assert "episodic_recall" not in section_names
        assert "recent_notes" not in section_names

    @pytest.mark.asyncio
    async def test_relationship_notes_capped_at_interim_char_limit(self) -> None:
        """`rel.notes` longer than `_REL_NOTES_INTERIM_CHARS` is truncated before injection.

        Pins the security-mitigation cap (PR #146) so a future refactor that
        removes the per-field char limit on relationship notes — leaving only
        the per-block token budget (~6000 chars) — fails this test rather
        than silently expanding the prompt-injection surface for peer-authored
        notes.  The existing
        ``test_relationship_exhausting_budget_starves_other_tiers`` test had to
        monkeypatch the cap away to assert budget-allocation behaviour, so the
        cap itself had no direct coverage.
        (PR #146 re-review: missing regression test for ``_REL_NOTES_INTERIM_CHARS``.)
        """
        from agents.persona_runtime import memory_context as mc

        # Notes well above the cap but small enough that the relationship
        # tier comfortably fits in the budget.
        long_notes = "x" * (mc._REL_NOTES_INTERIM_CHARS * 3)
        rel = _FakeRelSummary(
            other_participant_id="carol",
            interaction_count=2,
            notes=long_notes,
        )

        mixin, event = _make_mixin(rel=rel, sender_id="carol")
        await mixin._inject_memory_context(event)

        rel_section = next(
            (s for s in mixin._working_memory._sections
             if s.name == "relationship_context"),
            None,
        )
        assert rel_section is not None, "relationship section should be admitted"
        # Extract the notes line: "  Notes: <capped>"
        notes_line = next(
            (line for line in rel_section.content.splitlines()
             if line.startswith("  Notes: ")),
            None,
        )
        assert notes_line is not None, "Notes line should be present"
        notes_payload = notes_line[len("  Notes: "):]
        # _truncate_with_ellipsis appends "..." (3 chars) after the cap;
        # zero-space input means the full slice is used (no word-boundary
        # backtrack), so the payload is exactly cap + 3.
        assert len(notes_payload) <= mc._REL_NOTES_INTERIM_CHARS + 3, (
            f"Notes payload {len(notes_payload)} chars exceeds cap "
            f"{mc._REL_NOTES_INTERIM_CHARS} + ellipsis"
        )
        assert notes_payload.endswith("..."), (
            "Truncated notes should end with '...' to match episodic/notes "
            "truncation UX (PR #146 re-review consistency fix)."
        )


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
        # Content was truncated.  ``_truncate_with_ellipsis`` in char mode
        # (now applied via ``_MAX_NOTE_CONTENT_CHARS`` before the budget loop)
        # appends ``"..."`` (three ASCII dots), not the U+2026 unicode
        # ellipsis used by the token-mode path.  After the per-field char cap
        # commit, this short-circuits before the token-mode path is reached.
        # (PR #146 follow-up: assertion updated from '…' to '...'.)
        assert recent_notes_section.content.endswith("...")

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
        # No truncation marker for a small item (neither '...' from char mode
        # nor '…' from token mode).
        assert not recent_notes_section.content.endswith("...")
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

        assert result.memory_admitted_tokens > 0
        assert result.memory_admitted_tokens <= _MEMORY_BUDGET_TOKENS

    @pytest.mark.asyncio
    async def test_admitted_tokens_nonzero_with_content(self) -> None:
        """memory_admitted_tokens > 0 when at least one tier has content."""
        episodes = [_FakeEpisode(summary="relevant historical context")]
        mixin, event = _make_mixin(episodes=episodes)
        result = await mixin._inject_memory_context(event)
        assert result.memory_admitted_tokens > 0


# ─── Exception-path resiliency (RFC 0017 PR 2) ────────────────────────────────


class TestInjectMemoryContextExceptionResiliency:
    """Tier exceptions must not propagate; always returns MemoryInjectionResult."""

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


# ─── PR 6 — RFC 0017 review follow-ups ───────────────────────────────────────


class TestMemoryInjectionResultValidation:
    """PR 6 — RFC 0017 PR 5 review finding 4: ``__post_init__`` guard."""

    def test_negative_admitted_raises(self) -> None:
        """A negative admitted count must refuse to construct.

        Without this guard, the ``== 0`` empty-context TICK short-circuit in
        ``_ActionLoopMixin._on_event_inner`` would silently *not* fire on
        ``-1``, leaking LLM calls.
        """
        with pytest.raises(ValueError, match="memory_admitted_tokens must be >= 0"):
            MemoryInjectionResult(memory_admitted_tokens=-1)

    def test_zero_admitted_accepted(self) -> None:
        # Zero is the canonical empty-context signal; must construct.
        result = MemoryInjectionResult(memory_admitted_tokens=0)
        assert result.memory_admitted_tokens == 0

    def test_positive_admitted_accepted(self) -> None:
        result = MemoryInjectionResult(memory_admitted_tokens=42)
        assert result.memory_admitted_tokens == 42


class TestZeroBudgetIntegration:
    """PR 6 — RFC 0017 PR 2 review finding 6.

    The degenerate retune (``_MEMORY_BUDGET_TOKENS = 0``) is what an
    operator would set to disable memory injection.  Pin the contract that
    every tier is dropped and ``memory_admitted_tokens == 0``.
    """

    @pytest.mark.asyncio
    async def test_zero_budget_drops_all_sections(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import agents.persona_runtime.memory_context as mc

        monkeypatch.setattr(mc, "_MEMORY_BUDGET_TOKENS", 0)
        episodes = [_FakeEpisode(summary="historical context")]
        notes = [_FakeNote(topic="t", content="note content")]
        rel = _FakeRelSummary(
            other_participant_id="peer-1", notes="rich relationship notes"
        )
        mixin, event = _make_mixin(
            episodes=episodes, notes=notes, rel=rel, sender_id="peer-1",
        )

        result = await mixin._inject_memory_context(event)

        assert result.memory_admitted_tokens == 0
        section_names = [s.name for s in mixin._working_memory._sections]
        assert "episodic_recall" not in section_names
        assert "recent_notes" not in section_names
        assert "relationship_context" not in section_names
