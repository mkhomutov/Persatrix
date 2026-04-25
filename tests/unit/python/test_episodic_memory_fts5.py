"""
Tests for EpisodicMemory — FTS5 full-text search: availability, trigger sync,
ranking, malformed-query fallback, LIKE wildcard escaping, context retrieval,
and query sanitization.
"""

from unittest.mock import AsyncMock, patch

import pytest

from agents.memory.episodic import EpisodicMemory


# ─── FTS5 availability ──────────────────────────────────────


class TestFTS5:
    async def test_fts5_available(self, memory: EpisodicMemory):
        """FTS5 should be available in standard CPython builds."""
        assert memory._fts5 is True

    async def test_fts5_table_created(self, memory: EpisodicMemory):
        db = memory._ensure_db()
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='episodes_fts'"
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None

    async def test_fts5_triggers_created(self, memory: EpisodicMemory):
        db = memory._ensure_db()
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'episodes_a%'"
        ) as cursor:
            rows = await cursor.fetchall()
        trigger_names = {r[0] for r in rows}
        assert "episodes_ai" in trigger_names
        assert "episodes_ad" in trigger_names
        assert "episodes_au" in trigger_names

    async def test_fts5_fallback_when_unavailable(self):
        """When FTS5 is unavailable, memory falls back to LIKE queries."""
        mem = EpisodicMemory(agent_id="test-agent", db_path=":memory:")

        with patch("agents.memory.episodic._fts5_available", new_callable=AsyncMock) as mock_fts5:
            mock_fts5.return_value = False
            await mem.initialize()

        assert mem._fts5 is False

        # Store and recall should still work via LIKE fallback
        ep_id = await mem.store_episode(
            summary="test episode for LIKE fallback",
            context={"type": "test"},
        )
        results = await mem.recall("test episode")
        assert len(results) == 1
        assert results[0].id == ep_id

        await mem.close()


# ─── FTS5 trigger sync ─────────────────────────────────────


class TestFTS5TriggerSync:
    async def test_insert_syncs_to_fts(self, memory: EpisodicMemory):
        """Inserting an episode also inserts into FTS5 index."""
        await memory.store_episode(
            summary="FTS sync test about quantum computing",
            context={},
        )
        results = await memory.recall("quantum computing")
        assert len(results) == 1
        assert "quantum computing" in results[0].summary

    async def test_update_syncs_to_fts(self, memory: EpisodicMemory):
        """Updating an episode re-indexes in FTS5."""
        db = memory._ensure_db()
        ep_id = await memory.store_episode(
            summary="Original summary about machine learning",
            context={},
        )
        # Manually update the summary
        await db.execute(
            "UPDATE episodes SET summary = ? WHERE id = ?",
            ("Revised summary about deep learning", ep_id),
        )
        await db.commit()

        # Old term should not match
        await memory.recall("machine learning")
        new_results = await memory.recall("deep learning")

        # The new summary should be findable
        assert any("deep learning" in r.summary for r in new_results)

    async def test_delete_syncs_to_fts(self, memory: EpisodicMemory):
        """Deleting an episode removes it from FTS5 index."""
        db = memory._ensure_db()
        ep_id = await memory.store_episode(
            summary="Ephemeral data about blockchain",
            context={},
        )
        # Verify it's findable
        results = await memory.recall("blockchain")
        assert len(results) == 1

        # Delete it
        await db.execute("DELETE FROM episodes WHERE id = ?", (ep_id,))
        await db.commit()

        # Should no longer be findable
        results = await memory.recall("blockchain")
        assert len(results) == 0


# ─── FTS5 ranking ──────────────────────────────────────────


class TestFTS5Ranking:
    async def test_more_relevant_episodes_rank_higher(self, memory: EpisodicMemory):
        """Episodes with better FTS5 match and higher importance rank first."""
        await memory.store_episode(
            summary="Brief mention of python in a casual conversation",
            context={"type": "casual"},
            importance=0.3,
        )
        await memory.store_episode(
            summary="Extensive Python session with traceback analysis and Python profiling",
            context={"type": "deep-work", "language": "python"},
            importance=0.9,
        )
        # FTS5 MATCH on single term to hit both episodes
        results = await memory.recall("python")
        assert len(results) == 2
        # The more relevant + important episode should rank first
        assert results[0].importance == 0.9


# ─── FTS5 malformed query fallback ──────────────────────────


class TestFTS5MalformedQueryFallback:
    """FTS5 MATCH raises OperationalError on malformed syntax;
    _recall_fts5 must catch it and fall back to LIKE."""

    async def test_recall_lone_star(self, memory: EpisodicMemory):
        """A lone '*' is invalid FTS5 syntax — should fall back, not crash."""
        await memory.store_episode(summary="star test episode", context={})
        results = await memory.recall("*")
        # Falls back to LIKE '%*%' — no match expected, but no crash
        assert isinstance(results, list)

    async def test_recall_bare_not(self, memory: EpisodicMemory):
        """Bare 'NOT' is invalid FTS5 syntax — should fall back."""
        await memory.store_episode(summary="not test episode", context={})
        results = await memory.recall("NOT")
        assert isinstance(results, list)

    async def test_recall_unbalanced_quotes(self, memory: EpisodicMemory):
        """Unbalanced quotes are invalid FTS5 syntax — should fall back."""
        await memory.store_episode(summary="quote test episode", context={})
        results = await memory.recall('"unclosed')
        assert isinstance(results, list)

    async def test_recall_fts5_fallback_still_finds_via_like(self, memory: EpisodicMemory):
        """When FTS5 fails, LIKE fallback should still find matching episodes."""
        await memory.store_episode(
            summary="recipe for NOT burning toast",
            context={},
        )
        # "NOT" alone is invalid FTS5, but the episode summary contains "NOT"
        # so LIKE fallback with '%NOT%' should still match
        results = await memory.recall("NOT")
        assert len(results) >= 1
        assert "NOT" in results[0].summary


# ─── LIKE wildcard escaping ─────────────────────────────────


class TestLikeWildcardEscaping:
    """LIKE metacharacters %, _ in queries must be escaped so they
    match literally rather than acting as wildcards."""

    async def test_percent_in_query_does_not_match_all(self):
        """A query of '%' should not match every episode."""
        mem = EpisodicMemory(agent_id="test-like", db_path=":memory:")
        with patch("agents.memory.episodic._fts5_available", new_callable=AsyncMock) as mock_fts5:
            mock_fts5.return_value = False
            await mem.initialize()

        await mem.store_episode(summary="no percent sign here", context={})
        results = await mem.recall("%")
        assert len(results) == 0
        await mem.close()

    async def test_underscore_in_query_matches_literally(self):
        """A query of '_' should only match episodes containing literal '_'."""
        mem = EpisodicMemory(agent_id="test-like", db_path=":memory:")
        with patch("agents.memory.episodic._fts5_available", new_callable=AsyncMock) as mock_fts5:
            mock_fts5.return_value = False
            await mem.initialize()

        await mem.store_episode(summary="has_underscore in text", context={})
        await mem.store_episode(summary="no underscore meta here", context={})
        results = await mem.recall("_")
        assert len(results) == 1
        assert "_" in results[0].summary
        await mem.close()


# ─── FTS5 context-based retrieval ───────────────────────────


class TestFTS5ContextRetrieval:
    """FTS5 index covers both summary and context_json — verify
    that episodes can be found by terms appearing only in context."""

    async def test_fts5_finds_episode_by_context_term(self, memory: EpisodicMemory):
        """An episode with a distinctive term only in context (not summary)
        should still be findable via FTS5 search."""
        await memory.store_episode(
            summary="Routine task completion",
            context={"framework": "kubernetes", "cluster": "prod-east"},
        )
        await memory.store_episode(
            summary="Unrelated episode about databases",
            context={"type": "sql"},
        )
        results = await memory.recall("kubernetes")
        assert len(results) == 1
        assert results[0].context["framework"] == "kubernetes"


# ─── FTS5 query sanitization (episodic_queries) ─────────────


class TestEpisodicFTS5Sanitization:
    """Verify recall_fts5() sanitizes punctuation that causes FTS5 syntax errors.

    episodic_queries.recall_fts5() strips non-alphanumeric chars before
    passing the query to FTS5 MATCH, preventing OperationalError from
    commas, periods, angle brackets, pipes, etc.
    """

    async def test_comma_query_does_not_raise(self, memory: EpisodicMemory):
        """Comma in query must not cause FTS5 syntax error."""
        await memory.store_episode(
            summary="Team standup meeting notes", context={}, importance=0.5,
        )
        results = await memory.recall("hi, do you remember me?")
        assert isinstance(results, list)

    async def test_period_query_does_not_raise(self, memory: EpisodicMemory):
        """Period in query must not cause FTS5 syntax error."""
        await memory.store_episode(
            summary="Ship v2.0 on time with quality", context={}, importance=0.7,
        )
        results = await memory.recall("v2.0 timeline review.")
        assert isinstance(results, list)

    async def test_angle_brackets_query_does_not_raise(self, memory: EpisodicMemory):
        """Angle brackets and pipes (XML-style delimiters) do not cause errors."""
        await memory.store_episode(
            summary="User sent a greeting", context={}, importance=0.5,
        )
        results = await memory.recall('<|user_message user_id="local"|>')
        assert isinstance(results, list)

    async def test_sanitized_query_still_matches(self, memory: EpisodicMemory):
        """After stripping punctuation, alphanumeric tokens still match via FTS5."""
        await memory.store_episode(
            summary="Database schema migration completed",
            context={},
            importance=0.8,
        )
        await memory.store_episode(
            summary="Fixed CSS styling on login page",
            context={},
            importance=0.5,
        )
        # Comma and question mark stripped; "database" and "schema" remain.
        results = await memory.recall("database, schema?")
        assert len(results) >= 1
        assert "database" in results[0].summary.lower()

    async def test_only_punctuation_falls_back_to_recency(self, memory: EpisodicMemory):
        """A query of only punctuation (sanitizes to empty) falls back to recency.

        When the FTS5 sanitizer strips a query down to nothing, ``recall_fts5``
        short-circuits to ``recall_recency`` rather than ``recall_like`` —
        matching raw punctuation literally with LIKE rarely returns anything
        useful, while a recency/importance ranking still surfaces relevant
        episodes for the caller.
        """
        await memory.store_episode(
            summary="First episode", context={}, importance=0.5,
        )
        await memory.store_episode(
            summary="Second episode", context={}, importance=0.5,
        )
        results = await memory.recall(".,<>|!@#$%")
        assert isinstance(results, list)
        # Recency fallback should return both stored episodes regardless of
        # whether their summaries contain the punctuation chars.
        assert len(results) == 2
        summaries = {ep.summary for ep in results}
        assert summaries == {"First episode", "Second episode"}


class TestEpisodicFTS5SanitizeRegex:
    """Unit tests for the _FTS5_SANITIZE regex in episodic_queries.py."""

    def test_comma_stripped(self):
        from agents.memory.episodic_queries import _FTS5_SANITIZE
        assert _FTS5_SANITIZE.sub(" ", "hi, remember me?").strip() == "hi  remember me"

    def test_period_stripped(self):
        from agents.memory.episodic_queries import _FTS5_SANITIZE
        assert _FTS5_SANITIZE.sub(" ", "v2.0 timeline").strip() == "v2 0 timeline"

    def test_angle_brackets_stripped(self):
        from agents.memory.episodic_queries import _FTS5_SANITIZE
        result = _FTS5_SANITIZE.sub(" ", '<|user_message|>').strip()
        assert "<" not in result
        assert "|" not in result
        assert ">" not in result

    def test_plain_text_unchanged(self):
        from agents.memory.episodic_queries import _FTS5_SANITIZE
        assert _FTS5_SANITIZE.sub(" ", "database schema").strip() == "database schema"

    def test_only_special_chars_becomes_empty(self):
        from agents.memory.episodic_queries import _FTS5_SANITIZE
        assert _FTS5_SANITIZE.sub(" ", ".,!@#$%^&*()").strip() == ""
