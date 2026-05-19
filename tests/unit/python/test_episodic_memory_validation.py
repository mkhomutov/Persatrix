"""
Tests for EpisodicMemory — input validation and scoring internals: importance
clamping, summary/limit guards, zero-importance recall, ln() availability,
SQL score expression syntax, BM25 normalisation, and min_score range checks.
"""

import pytest

from agents.memory.episodic import EpisodicMemory
from agents.memory.episodic_queries import MAX_RECALL_LIMIT

# ─── Importance validation ──────────────────────────────────


class TestImportanceValidation:
    """store_episode() clamps importance to [0.0, 1.0]."""

    async def test_negative_importance_clamped(self, memory: EpisodicMemory):
        ep_id = await memory.store_episode(
            summary="Negative importance test",
            context={},
            importance=-0.5,
        )
        ep = await memory.get_episode(ep_id)
        assert ep is not None
        assert ep.importance == 0.0

    async def test_over_one_importance_clamped(self, memory: EpisodicMemory):
        ep_id = await memory.store_episode(
            summary="Over one importance test",
            context={},
            importance=1.5,
        )
        ep = await memory.get_episode(ep_id)
        assert ep is not None
        assert ep.importance == 1.0

    async def test_valid_importance_unchanged(self, memory: EpisodicMemory):
        ep_id = await memory.store_episode(
            summary="Normal importance test",
            context={},
            importance=0.7,
        )
        ep = await memory.get_episode(ep_id)
        assert ep is not None
        assert ep.importance == 0.7

    async def test_boundary_values_accepted(self, memory: EpisodicMemory):
        ep_id_zero = await memory.store_episode(
            summary="Zero importance", context={}, importance=0.0,
        )
        ep_id_one = await memory.store_episode(
            summary="One importance", context={}, importance=1.0,
        )
        ep_zero = await memory.get_episode(ep_id_zero)
        assert ep_zero is not None
        assert ep_zero.importance == 0.0
        ep_one = await memory.get_episode(ep_id_one)
        assert ep_one is not None
        assert ep_one.importance == 1.0


# ─── Summary validation ────────────────────────────────────


class TestSummaryValidation:
    """store_episode() rejects empty or whitespace-only summaries."""

    async def test_empty_summary_rejected(self, memory: EpisodicMemory):
        with pytest.raises(ValueError, match="summary must not be empty"):
            await memory.store_episode(summary="", context={})

    async def test_whitespace_only_summary_rejected(self, memory: EpisodicMemory):
        with pytest.raises(ValueError, match="summary must not be empty"):
            await memory.store_episode(summary="   \t\n  ", context={})

    async def test_valid_summary_accepted(self, memory: EpisodicMemory):
        ep_id = await memory.store_episode(summary="Valid summary", context={})
        assert ep_id is not None


# ─── Limit validation ──────────────────────────────────────


class TestLimitValidation:
    """recall() validates the limit parameter."""

    async def test_negative_limit_raises(self, memory: EpisodicMemory):
        with pytest.raises(ValueError, match="limit must be >= 1"):
            await memory.recall("anything", limit=-1)

    async def test_zero_limit_raises(self, memory: EpisodicMemory):
        with pytest.raises(ValueError, match="limit must be >= 1"):
            await memory.recall("anything", limit=0)

    async def test_limit_above_max_is_capped(self, memory: EpisodicMemory):
        """Limit exceeding MAX_RECALL_LIMIT is silently capped."""
        # Store a few episodes so we can verify the query still works
        for i in range(3):
            await memory.store_episode(summary=f"Limit cap episode {i}", context={})
        results = await memory.recall("", limit=MAX_RECALL_LIMIT + 50)
        # Should return all 3 (below the cap), proving the query ran without error
        assert len(results) == 3


# ─── Zero-importance recall (F-3a-1) ───────────────────────


class TestZeroImportanceRecall:
    async def test_zero_importance_episode_visible_in_recall(self, memory: EpisodicMemory):
        """importance=0.0 episodes must still appear in recall results (non-zero baseline)."""
        ep_id = await memory.store_episode(
            summary="Zero importance event",
            context={"detail": "test"},
            importance=0.0,
        )
        episodes = await memory.recall(query="", limit=10)
        ids = [e.id for e in episodes]
        assert ep_id in ids, (
            "Zero-importance episodes should be visible via non-zero scoring baseline"
        )


# ─── ln() availability check ───────────────────────────────


class TestLnAvailabilityCheck:
    """initialize() should raise RuntimeError when SQLite ln() is missing (R-02).

    Without ln(), the scoring formula in _SCORE_EXPR would cause all recall()
    calls to fail with a cryptic OperationalError. Failing at startup with a
    clear message is better than failing at query time.
    """

    async def test_initialize_raises_when_ln_unavailable(self):
        import sqlite3
        from unittest.mock import patch

        import aiosqlite

        real_connect = aiosqlite.connect

        class _FailOnLn:
            """Fake cursor that raises OperationalError on __aenter__."""

            async def __aenter__(self):
                raise sqlite3.OperationalError("no such function: ln")

            async def __aexit__(self, *a):
                pass

            def __await__(self):
                return self.__aenter__().__await__()

        class _LnFailDB:
            """Wraps aiosqlite.Connection, failing only the ln(1) query."""

            def __init__(self, db):
                self._db = db

            def execute(self, sql, *args, **kwargs):
                if "ln(1)" in sql:
                    return _FailOnLn()
                return self._db.execute(sql, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(self._db, name)

        wrapped_db = None

        async def patched_connect(*a, **kw):
            nonlocal wrapped_db
            db = await real_connect(*a, **kw)
            wrapped_db = _LnFailDB(db)
            return wrapped_db

        mem = EpisodicMemory(agent_id="test", db_path=":memory:")
        with patch("aiosqlite.connect", side_effect=patched_connect):
            with pytest.raises(RuntimeError, match="ln.*not available"):
                await mem.initialize()

        # Clean up the underlying real connection
        if wrapped_db is not None:
            await wrapped_db._db.close()


# ─── SQL scoring expression syntax (R-01) ──────────────────


class TestScoreExpressionSyntax:
    """Verify _SCORE_EXPR and _SCORE_EXPR_BARE produce valid SQL.

    A typo in _SCORE_TEMPLATE would only surface at recall time. This test
    catches template formatting errors at test time by executing the generated
    SQL fragments against an in-memory database (R-01).
    """

    async def test_score_expr_is_valid_sql(self, memory):
        """_SCORE_EXPR (table-aliased) should be syntactically valid SQL."""
        from agents.memory.migrations import _SCORE_EXPR

        # Execute inside a SELECT against the real episodes table (aliased as e)
        # with a concrete time parameter to exercise the full expression.
        sql = f"SELECT {_SCORE_EXPR} FROM episodes e WHERE e.agent_id = ? LIMIT 1"
        import time

        async with memory._db.execute(sql, (time.time(), "test-agent")) as cursor:
            await cursor.fetchone()  # No OperationalError = valid SQL

    async def test_score_expr_bare_is_valid_sql(self, memory):
        """_SCORE_EXPR_BARE (no table prefix) should be syntactically valid SQL."""
        from agents.memory.migrations import _SCORE_EXPR_BARE

        sql = f"SELECT {_SCORE_EXPR_BARE} FROM episodes WHERE agent_id = ? LIMIT 1"
        import time

        async with memory._db.execute(sql, (time.time(), "test-agent")) as cursor:
            await cursor.fetchone()


# ─── _normalize_bm25 unit tests (RFC 0017 §C) ─────────────


class TestNormalizeBm25:
    """Unit tests for the _normalize_bm25 helper in episodic_queries.

    FTS5 rank is a negative BM25 value (more-negative = more relevant).
    The normalised score is 1.0 / (1.0 + abs(raw)), clamped to [0, 1].
    """

    def test_none_returns_zero(self):
        from agents.memory.episodic_queries import _normalize_bm25
        assert _normalize_bm25(None) == 0.0

    def test_zero_returns_zero(self):
        from agents.memory.episodic_queries import _normalize_bm25
        assert _normalize_bm25(0.0) == 0.0

    def test_negative_one_returns_half(self):
        from agents.memory.episodic_queries import _normalize_bm25
        assert _normalize_bm25(-1.0) == pytest.approx(0.5, abs=1e-9)

    def test_positive_one_returns_half(self):
        """Positive raw scores are treated symmetrically (abs)."""
        from agents.memory.episodic_queries import _normalize_bm25
        assert _normalize_bm25(1.0) == pytest.approx(0.5, abs=1e-9)

    def test_very_negative_approaches_zero(self):
        from agents.memory.episodic_queries import _normalize_bm25
        score = _normalize_bm25(-1000.0)
        assert score == pytest.approx(0.001, abs=1e-3)
        assert score >= 0.0

    def test_small_negative_approaches_one(self):
        from agents.memory.episodic_queries import _normalize_bm25
        # rank = -0.001 → 1/(1.001) ≈ 0.999
        score = _normalize_bm25(-0.001)
        assert score > 0.99
        assert score <= 1.0

    def test_result_in_unit_interval(self):
        from agents.memory.episodic_queries import _normalize_bm25
        for raw in [None, 0.0, -0.5, -1.0, -5.0, -50.0, 0.5, 5.0]:
            score = _normalize_bm25(raw)
            assert 0.0 <= score <= 1.0, f"score={score} out of [0,1] for raw={raw}"


# ─── min_score range validation (PR #147 review) ───────────


class TestMinScoreRangeValidation:
    """min_score must be in [0.0, 1.0] or None.

    Out-of-range values silently no-op (negative) or filter everything
    (>1.0), making misconfiguration hard to debug in production.  Both
    EpisodicMemory.recall() and NoteStore.recall_notes() validate at
    the public boundary, mirroring the existing `limit` guard.
    """

    @pytest.mark.parametrize("bad_value", [-0.1, -1.0, 1.1, 2.0])
    async def test_recall_rejects_out_of_range_min_score(
        self, memory: EpisodicMemory, bad_value: float,
    ):
        with pytest.raises(ValueError, match="min_score must be in"):
            await memory.recall("anything", min_score=bad_value)

    @pytest.mark.parametrize("bad_value", [-0.1, -1.0, 1.1, 2.0])
    async def test_recall_notes_rejects_out_of_range_min_score(
        self, memory: EpisodicMemory, bad_value: float,
    ):
        with pytest.raises(ValueError, match="min_score must be in"):
            await memory.recall_notes("anything", min_score=bad_value)

    @pytest.mark.parametrize("ok_value", [None, 0.0, 0.5, 1.0])
    async def test_recall_accepts_boundary_min_score(
        self, memory: EpisodicMemory, ok_value: float | None,
    ):
        # Should not raise; result set may be empty for an empty corpus.
        await memory.recall("anything", min_score=ok_value)

    @pytest.mark.parametrize("ok_value", [None, 0.0, 0.5, 1.0])
    async def test_recall_notes_accepts_boundary_min_score(
        self, memory: EpisodicMemory, ok_value: float | None,
    ):
        await memory.recall_notes("anything", min_score=ok_value)


# ─── Default constants exposed (RFC 0017 §C) ───────────────


class TestMinScoreDefaults:
    """Verify the per-tier default constants are accessible and in range."""

    def test_default_episodic_min_score_in_range(self):
        from agents.memory.episodic import DEFAULT_EPISODIC_MIN_SCORE
        assert 0.0 <= DEFAULT_EPISODIC_MIN_SCORE <= 1.0

    def test_default_notes_min_score_in_range(self):
        from agents.memory.episodic import DEFAULT_NOTES_MIN_SCORE
        assert 0.0 <= DEFAULT_NOTES_MIN_SCORE <= 1.0

    def test_defaults_are_floats(self):
        from agents.memory.episodic import (
            DEFAULT_EPISODIC_MIN_SCORE,
            DEFAULT_NOTES_MIN_SCORE,
        )
        assert isinstance(DEFAULT_EPISODIC_MIN_SCORE, float)
        assert isinstance(DEFAULT_NOTES_MIN_SCORE, float)
