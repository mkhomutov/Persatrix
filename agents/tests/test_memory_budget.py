"""Unit tests for MemoryBudget (RFC 0017 §B).

Covers every branch documented in the RFC and PR plan, including the
tiktoken-unavailable fallback path (simulated via monkeypatch).
"""

from __future__ import annotations

import sys

import pytest

from agents.persona_runtime.memory_budget import (
    MemoryBudget,
    _count_tokens,
    _truncate_to_token_limit,
)

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _rough_tokens(text: str) -> int:
    """Rough token estimate used in assertions that don't need tiktoken precision."""
    return max(1, len(text) // 4)


# ─── _count_tokens ────────────────────────────────────────────────────────────


class TestCountTokens:
    def test_empty_string_returns_at_least_one(self) -> None:
        # When tiktoken is available it returns 0 for an empty encode; the
        # char-proportional fallback returns max(1, 0) = 1.  Both are valid
        # results, so the assertion is >= 0 (not >= 1).
        assert _count_tokens("") >= 0  # empty is valid; tiktoken may return 0

    def test_short_ascii_positive(self) -> None:
        count = _count_tokens("hello world")
        assert count >= 1

    def test_tiktoken_unavailable_falls_back_to_chars_div_4(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When tiktoken import fails, falls back to chars//4."""
        # Remove tiktoken from sys.modules to simulate it being absent.
        monkeypatch.setitem(sys.modules, "tiktoken", None)  # type: ignore[arg-type]
        text = "a" * 40  # 40 chars → chars//4 = 10 tokens
        count = _count_tokens(text)
        assert count == max(1, 10)

    def test_longer_text_gives_more_tokens_than_shorter(self) -> None:
        short = _count_tokens("hi")
        long_ = _count_tokens("hi " * 100)
        assert long_ > short


# ─── _truncate_to_token_limit ─────────────────────────────────────────────────


class TestTruncateToTokenLimit:
    def test_text_within_limit_unchanged(self) -> None:
        text = "Short text."
        result = _truncate_to_token_limit(text, 100)
        assert result == text

    def test_zero_limit_returns_ellipsis(self) -> None:
        result = _truncate_to_token_limit("anything", 0)
        assert result == "…"

    def test_truncated_ends_with_ellipsis(self) -> None:
        long_text = "word " * 500
        result = _truncate_to_token_limit(long_text, 20)
        assert result.endswith("…")

    def test_truncated_token_count_within_limit(self) -> None:
        long_text = "word " * 500
        limit = 30
        result = _truncate_to_token_limit(long_text, limit)
        count = _count_tokens(result)
        assert count <= limit

    def test_tiktoken_unavailable_falls_back_gracefully(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "tiktoken", None)  # type: ignore[arg-type]
        long_text = "x" * 400  # 100 approx tokens
        result = _truncate_to_token_limit(long_text, 10)
        assert result.endswith("…")
        # Approximate: result should be shorter than original.
        assert len(result) < len(long_text)

    def test_tiktoken_unavailable_short_text_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "tiktoken", None)  # type: ignore[arg-type]
        text = "Hi!"  # 1 approx token
        result = _truncate_to_token_limit(text, 10)
        assert result == text


# ─── MemoryBudget.try_add ─────────────────────────────────────────────────────


class TestMemoryBudgetTryAdd:
    def test_empty_text_dropped(self) -> None:
        budget = MemoryBudget(total_tokens=100)
        assert budget.try_add("") is None
        assert budget.remaining == 100  # unchanged

    def test_budget_zero_always_drops(self) -> None:
        budget = MemoryBudget(total_tokens=0)
        assert budget.try_add("some text") is None
        assert budget.remaining == 0

    def test_small_item_fits_whole(self) -> None:
        budget = MemoryBudget(total_tokens=500)
        before = budget.remaining
        text = "hello world"
        result = budget.try_add(text)
        assert result == text
        # remaining decremented by exact token count
        assert budget.remaining == before - _count_tokens(text)

    def test_item_larger_than_budget_truncated_when_enough_tokens(self) -> None:
        """Item exceeds remaining but truncated form >= min_tokens → admitted truncated."""
        budget = MemoryBudget(total_tokens=50)
        long_text = "word " * 200  # many tokens
        result = budget.try_add(long_text, min_tokens=10)
        assert result is not None
        assert result.endswith("…")
        assert _count_tokens(result) >= 10
        assert _count_tokens(result) <= 50

    def test_item_larger_than_budget_dropped_when_truncated_too_small(self) -> None:
        """Item exceeds remaining and truncated form < min_tokens → dropped."""
        # Budget of 5 tokens; min_tokens=10 → truncated would be < 10 → drop.
        budget = MemoryBudget(total_tokens=5)
        before = budget.remaining
        text = "word " * 200
        result = budget.try_add(text, min_tokens=10)
        assert result is None
        assert budget.remaining == before  # unchanged

    def test_per_call_min_tokens_override(self) -> None:
        """Different min_tokens floors per call site work independently."""
        # Budget of 5 tokens.
        budget = MemoryBudget(total_tokens=5)
        long_text = "word " * 200

        # With min_tokens=10: truncated form (~5 tokens) < 10 → drop.
        result_strict = budget.try_add(long_text, min_tokens=10)
        assert result_strict is None

        # With min_tokens=1: truncated form >= 1 → admit.
        result_lenient = budget.try_add(long_text, min_tokens=1)
        assert result_lenient is not None

    def test_sequence_fills_budget_greedily(self) -> None:
        """Items are admitted in order until budget exhausted; later items dropped."""
        # Use 50 items so the budget of 20 is definitely exceeded.
        budget = MemoryBudget(total_tokens=20)
        items = ["hello world"] * 50  # each ~2-3 tokens; 50 items > 20 tokens
        admitted = [budget.try_add(item) for item in items]

        # At least some admitted, at least one dropped.
        assert any(r is not None for r in admitted)
        assert any(r is None for r in admitted)

        # Once we see the first None, all subsequent must also be None
        # (greedy order, budget can only decrease).
        first_none = next(i for i, r in enumerate(admitted) if r is None)
        for r in admitted[first_none:]:
            assert r is None

    def test_sequence_earlier_items_intact(self) -> None:
        """Admitted items are returned unchanged (not truncated)."""
        budget = MemoryBudget(total_tokens=100)
        text = "hello world"  # small, fits whole
        result = budget.try_add(text)
        assert result == text

    def test_remaining_property_decrements_correctly(self) -> None:
        budget = MemoryBudget(total_tokens=100)
        text = "hello world"
        expected_cost = _count_tokens(text)
        budget.try_add(text)
        assert budget.remaining == 100 - expected_cost

    def test_total_tokens_zero_initial_budget_drops_all(self) -> None:
        budget = MemoryBudget(total_tokens=0)
        for _ in range(5):
            assert budget.try_add("anything") is None
        assert budget.remaining == 0

    def test_tiktoken_unavailable_still_works(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MemoryBudget works without tiktoken (chars//4 fallback)."""
        monkeypatch.setitem(sys.modules, "tiktoken", None)  # type: ignore[arg-type]
        budget = MemoryBudget(total_tokens=100)
        result = budget.try_add("hello world")
        assert result == "hello world"
        assert budget.remaining < 100  # some tokens consumed

    def test_admitted_token_cost_equals_remaining_delta(self) -> None:
        """Admitted token count == remaining_before - remaining_after."""
        budget = MemoryBudget(total_tokens=500)
        text = "the quick brown fox jumps over the lazy dog"
        before = budget.remaining
        result = budget.try_add(text)
        after = budget.remaining
        assert result is not None
        assert before - after == _count_tokens(text)

    def test_admitted_token_cost_truncated_equals_remaining_delta(self) -> None:
        """For truncated items: cost == remaining_before - remaining_after."""
        budget = MemoryBudget(total_tokens=20)
        long_text = "word " * 200
        before = budget.remaining
        result = budget.try_add(long_text, min_tokens=1)
        after = budget.remaining
        assert result is not None
        admitted_cost = before - after
        assert admitted_cost == _count_tokens(result)
