"""
Tests for WorkingMemory — context window management with
priority-weighted retention and automatic summarization.

All tests use mock LLM client — no real API calls.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.llm_client import LLMClient, LLMResponse, Usage
from agents.memory.working import ContextSection, WorkingMemory, estimate_tokens
from agents.memory import MemoryLifecycle


# ─── Fixtures ───────────────────────────────────────────────


def _make_section(
    name: str = "test",
    content: str = "test content",
    priority: int = 50,
    token_count: int = 100,
    compressible: bool = True,
) -> ContextSection:
    return ContextSection(
        name=name,
        content=content,
        priority=priority,
        token_count=token_count,
        compressible=compressible,
    )


def _make_llm_client(summary: str = "compressed summary") -> LLMClient:
    """Create a mock LLMClient that returns a summary response."""
    mock_provider = AsyncMock()
    mock_provider.create_message = AsyncMock(
        return_value=LLMResponse(text=summary, usage=Usage(10, 20))
    )
    mock_provider.format_tool_definitions = MagicMock(return_value=[])
    mock_provider.append_tool_round = MagicMock()
    return LLMClient(mock_provider)


# ─── estimate_tokens ────────────────────────────────────────


class TestEstimateTokens:
    def test_ascii_text(self):
        text = "Hello, world!"  # 13 chars → 3 tokens
        assert estimate_tokens(text) == 13 // 4

    def test_empty_string(self):
        assert estimate_tokens("") == 0

    def test_short_string(self):
        # Strings shorter than 4 chars still return 0
        assert estimate_tokens("abc") == 0

    def test_long_string(self):
        text = "a" * 1000
        assert estimate_tokens(text) == 250

    def test_mixed_content(self):
        # JSON/code-like content
        text = '{"key": "value", "nested": {"inner": true}}'
        assert estimate_tokens(text) == len(text) // 4

    def test_accurate_false_is_default(self):
        text = "Hello, world!"
        assert estimate_tokens(text) == estimate_tokens(text, accurate=False)

    def test_accurate_true_fallback(self):
        # Even if tiktoken is unavailable, accurate=True falls back to chars/4
        text = "Hello, world!"
        result = estimate_tokens(text, accurate=True)
        # Should return a reasonable number (exact value depends on tiktoken availability)
        assert result >= 0


# ─── Tiktoken conditional test (F-2-4) ─────────────────────


class TestEstimateTokensTiktoken:
    def test_accurate_with_tiktoken(self):
        """When tiktoken IS available, accurate=True uses it and differs from chars/4."""
        pytest.importorskip("tiktoken")
        text = "Hello, world! This is a longer sentence for testing token estimation."
        accurate = estimate_tokens(text, accurate=True)
        naive = len(text) // 4
        assert accurate != naive, "accurate path should differ from chars/4 when tiktoken is available"
        assert accurate > 0

    def test_accurate_true_fallback_when_tiktoken_unavailable(self):
        """When tiktoken import fails, accurate=True falls back to chars/4.

        Mocks the import to guarantee the fallback path is exercised even
        when tiktoken IS installed in the test environment (PR #59 review).
        """
        text = "Hello, world!"
        with patch.dict("sys.modules", {"tiktoken": None}):
            result = estimate_tokens(text, accurate=True)
        assert result == len(text) // 4

    def test_accurate_true_fallback_when_tiktoken_broken(self):
        """When tiktoken is installed but encoding fails, falls back to chars/4.

        Covers the broader Exception catch added for defensive robustness
        (PR #59 review: corrupted install, C extension failure, etc.).
        """
        text = "Hello, world!"
        fake_tiktoken = MagicMock()
        fake_tiktoken.get_encoding.side_effect = RuntimeError("encoding registry corrupt")
        with patch.dict("sys.modules", {"tiktoken": fake_tiktoken}):
            result = estimate_tokens(text, accurate=True)
        assert result == len(text) // 4


# ─── WorkingMemory.add_section ──────────────────────────────


class TestAddSection:
    def test_add_section(self):
        wm = WorkingMemory()
        wm.add_section(_make_section(name="system", token_count=50))
        assert wm.total_tokens() == 50

    def test_add_multiple_sections(self):
        wm = WorkingMemory()
        wm.add_section(_make_section(name="system", token_count=50))
        wm.add_section(_make_section(name="persona", token_count=30))
        assert wm.total_tokens() == 80

    def test_replace_existing_section(self):
        wm = WorkingMemory()
        wm.add_section(_make_section(name="system", token_count=50))
        wm.add_section(_make_section(name="system", token_count=70))
        assert wm.total_tokens() == 70

    def test_replace_preserves_other_sections(self):
        wm = WorkingMemory()
        wm.add_section(_make_section(name="system", token_count=50))
        wm.add_section(_make_section(name="persona", token_count=30))
        wm.add_section(_make_section(name="system", token_count=70))
        assert wm.total_tokens() == 100  # 70 + 30


# ─── WorkingMemory.remove_section / get_section ─────────────


class TestSectionAccess:
    def test_get_section(self):
        wm = WorkingMemory()
        section = _make_section(name="system", content="sys prompt")
        wm.add_section(section)
        assert wm.get_section("system") is not None
        assert wm.get_section("system").content == "sys prompt"

    def test_get_missing_section(self):
        wm = WorkingMemory()
        assert wm.get_section("nonexistent") is None

    def test_remove_section(self):
        wm = WorkingMemory()
        wm.add_section(_make_section(name="system", token_count=50))
        wm.remove_section("system")
        assert wm.total_tokens() == 0

    def test_remove_nonexistent_is_noop(self):
        wm = WorkingMemory()
        wm.add_section(_make_section(name="system", token_count=50))
        wm.remove_section("nonexistent")
        assert wm.total_tokens() == 50


# ─── WorkingMemory.total_tokens ─────────────────────────────


class TestTotalTokens:
    def test_empty(self):
        wm = WorkingMemory()
        assert wm.total_tokens() == 0

    def test_accumulates(self):
        wm = WorkingMemory()
        wm.add_section(_make_section(name="a", token_count=100))
        wm.add_section(_make_section(name="b", token_count=200))
        wm.add_section(_make_section(name="c", token_count=300))
        assert wm.total_tokens() == 600


# ─── WorkingMemory.max_tokens property ──────────────────────


class TestMaxTokens:
    def test_default(self):
        wm = WorkingMemory()
        assert wm.max_tokens == 100_000

    def test_custom(self):
        wm = WorkingMemory(max_tokens=50_000)
        assert wm.max_tokens == 50_000


# ─── WorkingMemory.build_context ────────────────────────────


class TestBuildContext:
    def test_priority_order(self):
        wm = WorkingMemory(max_tokens=1000)
        wm.add_section(_make_section(name="low", priority=10, token_count=100))
        wm.add_section(_make_section(name="high", priority=100, token_count=100))
        wm.add_section(_make_section(name="mid", priority=50, token_count=100))
        context = wm.build_context()
        names = [c["role"] for c in context]
        assert names == ["high", "mid", "low"]

    def test_drops_overflow_sections(self):
        wm = WorkingMemory(max_tokens=150)
        wm.add_section(_make_section(name="high", priority=100, token_count=100))
        wm.add_section(_make_section(name="low", priority=10, token_count=100))
        context = wm.build_context()
        assert len(context) == 1
        assert context[0]["role"] == "high"

    def test_empty_context(self):
        wm = WorkingMemory()
        assert wm.build_context() == []

    def test_all_sections_fit(self):
        wm = WorkingMemory(max_tokens=500)
        wm.add_section(_make_section(name="a", priority=50, token_count=100))
        wm.add_section(_make_section(name="b", priority=80, token_count=100))
        context = wm.build_context()
        assert len(context) == 2

    def test_content_preserved(self):
        wm = WorkingMemory(max_tokens=500)
        wm.add_section(
            _make_section(name="system", content="You are a helpful agent", token_count=50)
        )
        context = wm.build_context()
        assert context[0]["content"] == "You are a helpful agent"

    def test_exact_budget_boundary(self):
        wm = WorkingMemory(max_tokens=200)
        wm.add_section(_make_section(name="a", priority=100, token_count=100))
        wm.add_section(_make_section(name="b", priority=50, token_count=100))
        context = wm.build_context()
        assert len(context) == 2  # exactly at budget

    def test_skips_large_section_includes_smaller(self):
        """Greedy bin-packing: a mid-priority section overflows but a smaller low-priority one fits."""
        wm = WorkingMemory(max_tokens=200)
        wm.add_section(_make_section(name="high", priority=100, token_count=150))
        wm.add_section(_make_section(name="mid", priority=50, token_count=100))  # overflows
        wm.add_section(_make_section(name="low", priority=10, token_count=40))  # fits
        context = wm.build_context()
        names = [c["role"] for c in context]
        assert "high" in names
        assert "mid" not in names
        assert "low" in names


# ─── MemoryLifecycle protocol (F-2-5) ──────────────────────


class TestInitialize:
    async def test_initialize_is_noop(self):
        """WorkingMemory.initialize() exists and is a no-op (MemoryLifecycle contract)."""
        wm = WorkingMemory()
        wm.add_section(_make_section(name="a", token_count=50))
        await wm.initialize()
        # State is unchanged
        assert wm.total_tokens() == 50

    def test_working_memory_satisfies_memory_lifecycle(self):
        """WorkingMemory structurally matches @runtime_checkable MemoryLifecycle (R-10)."""
        assert isinstance(WorkingMemory(), MemoryLifecycle)
