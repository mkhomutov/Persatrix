"""Unit tests for _truncate_with_ellipsis in memory_context.py (RFC 0017 §D).

Split from test_persona_runtime_memory_context.py when that file exceeded
the 500-line code size limit.
"""

from __future__ import annotations

import sys

import pytest

from agents.persona_runtime.memory_budget import _count_tokens
from agents.persona_runtime.memory_context import _truncate_with_ellipsis

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
