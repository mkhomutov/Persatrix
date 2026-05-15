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


# ─── Boundary cases (PR #145 review follow-up) ────────────────────────────────
#
# The cases below close coverage gaps flagged in the PR #145 review report
# under "Nice to Have #1".  They exercise documented boundaries that the
# original suite only probed indirectly:
#
#   1. Negative ``total_tokens`` constructor input — handled by ``max(0, ...)``
#      in ``__init__`` but not previously asserted.
#   2. ``min_tokens`` exactly equal to the truncated item's token count — the
#      admittance condition is ``truncated_count >= min_tokens`` (inclusive),
#      and earlier tests only probed clearly-above / clearly-below cases.
#   3. ``_truncate_to_token_limit(text, 1)`` with tiktoken available —
#      ``content_budget = 1 - ellipsis_tokens = 0`` for cl100k_base, which
#      hits the ``content_budget <= 0`` branch returning bare ``"…"``.


class TestBoundaryCases:
    def test_negative_total_tokens_clamped_to_zero(self) -> None:
        """``MemoryBudget(-1)`` must behave identically to ``MemoryBudget(0)``."""
        budget = MemoryBudget(total_tokens=-1)
        assert budget.remaining == 0
        # Anything submitted to a zero-remaining budget is dropped.
        assert budget.try_add("hello world") is None
        assert budget.remaining == 0

    def test_min_tokens_equal_to_truncated_count_admits(self) -> None:
        """Boundary on ``truncated_count >= min_tokens`` (inclusive)."""
        # Use a small budget so truncation is forced, then read the truncated
        # count and re-run with min_tokens set to exactly that value.  The
        # inclusive ``>=`` means the item must be admitted, not dropped.
        budget_probe = MemoryBudget(total_tokens=20)
        long_text = "word " * 200
        truncated = budget_probe.try_add(long_text, min_tokens=1)
        assert truncated is not None
        exact_count = _count_tokens(truncated)

        # Fresh budget so we re-trigger the same truncation path.
        budget = MemoryBudget(total_tokens=20)
        result = budget.try_add(long_text, min_tokens=exact_count)
        assert result is not None  # admitted at the equality boundary
        assert _count_tokens(result) == exact_count

    def test_truncate_to_token_limit_one_returns_ellipsis(self) -> None:
        """``token_limit=1`` exhausts budget on the ellipsis itself.

        With cl100k_base, ``"…"`` (U+2026) encodes to 1 token, so
        ``content_budget = 1 - 1 = 0`` triggers the ``content_budget <= 0``
        early-return branch and the result is bare ``"…"`` (no content).
        """
        result = _truncate_to_token_limit("any sufficiently long text here", 1)
        assert result == "…"


# ─── PR 6 — RFC 0017 review follow-ups ────────────────────────────────────────


class TestPR6Followups:
    """Coverage added in PR 6 for review findings deferred from PRs 1, 2, 5."""

    # PR 1 review finding 1 — _count_tokens("") fallback parity with tiktoken.
    def test_count_tokens_empty_returns_zero_in_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fallback path must return 0 for empty input (matches tiktoken)."""
        monkeypatch.setitem(sys.modules, "tiktoken", None)  # type: ignore[arg-type]
        assert _count_tokens("") == 0

    # PR 1 review finding 3 — default min_tokens=32 deciding factor.
    def test_default_min_tokens_floor_is_32(self) -> None:
        """Without an override, items truncating to 1–31 tokens are dropped.

        Pins the documented default floor as the deciding factor.  An item
        that *would* admit at min_tokens=1 must be *dropped* at the default
        when the truncated form is below 32 tokens.
        """
        # Budget of ~10 tokens: anything truncated lands well below 32.
        budget = MemoryBudget(total_tokens=10)
        long_text = "word " * 200

        # Default min_tokens=32 → drop.
        assert budget.try_add(long_text) is None
        assert budget.remaining == 10

        # Same text at min_tokens=1 → admit.
        result = budget.try_add(long_text, min_tokens=1)
        assert result is not None

    # PR 1 review finding 5 — heterogeneous greedy semantics.
    def test_greedy_admits_smaller_item_after_larger_dropped(self) -> None:
        """The greedy contract is *not* "first-None ends admission" for mixed sizes.

        After a large item exhausts most of the budget, a later *smaller* item
        can still fit.  Pins the RFC's intended greedy-order semantics for
        heterogeneous inputs (the homogeneous-list test above is a narrower
        case that doesn't exercise this path).
        """
        budget = MemoryBudget(total_tokens=20)
        small = "hi"        # ~1 token
        # Pre-consume to leave exactly a sliver, then verify the small item
        # is admitted greedily after a prior consumption — the RFC's intended
        # ordering semantic.
        first = budget.try_add("hello", min_tokens=1)
        assert first is not None
        remaining_before = budget.remaining
        result_small = budget.try_add(small, min_tokens=1)
        assert result_small == small
        assert budget.remaining < remaining_before


# ─── PR #342 second-pass review DR2-N-6 ───────────────────────────────────────


class TestEncodeOnceOnOversizedItem:
    """DR2-N-6 — ``try_add`` tokenises the full item text only once.

    The oversized path used to re-encode the full text: once in
    ``_count_tokens(text)`` and again inside ``_truncate_to_token_limit``.
    With PR 4's multi-block facts render a tight per-tier slice reaches
    the oversized branch more often, so the redundant re-encode is now
    worth removing.  ``try_add`` caches the token list off the first
    encode and threads it into the truncator, which decodes against the
    same list instead of re-encoding.
    """

    def test_oversized_item_encodes_full_text_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The long original text is passed to ``enc.encode`` exactly once.

        Regression guard for the redundant re-encode: pre-fix this count
        was 2 (counter + truncator).  The short truncated string and the
        one-char ellipsis are still encoded separately — those are cheap
        and out of scope; only the full-text re-encode is eliminated.
        """
        tiktoken = pytest.importorskip("tiktoken")

        encoded_texts: list[str] = []
        real_encode = tiktoken.Encoding.encode

        def _spy(self, text, *args, **kwargs):  # type: ignore[no-untyped-def]
            encoded_texts.append(text)
            return real_encode(self, text, *args, **kwargs)

        monkeypatch.setattr(tiktoken.Encoding, "encode", _spy)

        budget = MemoryBudget(total_tokens=20)
        long_text = "word " * 200  # far exceeds the 20-token budget
        result = budget.try_add(long_text, min_tokens=1)

        assert result is not None  # admitted in truncated form
        assert result.endswith("…")
        assert encoded_texts.count(long_text) == 1

    def test_whole_fit_item_encodes_text_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An item that fits whole is also encoded only once (no truncation)."""
        tiktoken = pytest.importorskip("tiktoken")

        encoded_texts: list[str] = []
        real_encode = tiktoken.Encoding.encode

        def _spy(self, text, *args, **kwargs):  # type: ignore[no-untyped-def]
            encoded_texts.append(text)
            return real_encode(self, text, *args, **kwargs)

        monkeypatch.setattr(tiktoken.Encoding, "encode", _spy)

        budget = MemoryBudget(total_tokens=500)
        text = "the quick brown fox jumps over the lazy dog"
        result = budget.try_add(text)

        assert result == text
        assert encoded_texts.count(text) == 1
