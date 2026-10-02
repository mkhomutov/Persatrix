"""
Tests for EpisodicMemory — min_score filtering: FTS5 threshold behaviour,
LIKE-fallback passthrough, notes tier, helper contracts, and the has_fts5
public property.
"""

from unittest.mock import AsyncMock, patch

import pytest

from agents.memory.episodic import EpisodicMemory

# ─── recall(min_score=...) — FTS5 filtering (RFC 0017 §C) ──


class TestRecallMinScore:
    """Tests for the min_score parameter on EpisodicMemory.recall().

    min_score is relative (ISSUE-0159): a row stays when its bm25 relevance
    is at least min_score times the best relevance among the call's
    candidates, applied before limit. None and 0.0 mean no floor; 1.0 keeps
    the best match and its ties. LIKE-fallback treats all matches as score
    1.0 (no filtering).
    """

    async def test_min_score_none_same_as_default(self, memory: EpisodicMemory):
        """min_score=None produces identical results to omitting the parameter."""
        await memory.store_episode(
            summary="deep work session on microservices architecture",
            context={"type": "work"},
            importance=0.8,
        )
        results_default = await memory.recall("microservices architecture")
        results_none = await memory.recall("microservices architecture", min_score=None)
        assert len(results_none) == len(results_default)
        assert {ep.id for ep in results_none} == {ep.id for ep in results_default}

    async def test_min_score_zero_admits_all_fts5_matches(self, memory: EpisodicMemory):
        """min_score=0.0 passes every FTS5 match (every share of the best is >= 0)."""
        for i in range(3):
            await memory.store_episode(
                summary=f"xenolith geology expedition number {i}",
                context={"idx": i},
            )
        results = await memory.recall("xenolith geology", min_score=0.0)
        assert len(results) == 3

    async def test_min_score_one_keeps_only_the_best_match(self, memory: EpisodicMemory):
        """min_score=1.0 keeps the best match and drops the weaker one."""
        target = await memory.store_episode(
            summary="bioluminescent plankton coastal observation",
            context={},
            importance=0.9,
        )
        await memory.store_episode(summary="plankton net maintenance", context={})
        await memory.store_episode(summary="harbour weather log", context={})
        results = await memory.recall("bioluminescent plankton", min_score=1.0)
        assert [ep.id for ep in results] == [target]
        assert len(await memory.recall("bioluminescent plankton", min_score=0.0)) == 2

    async def test_min_score_filters_before_limit(self, memory: EpisodicMemory):
        """When min_score drops items, limit is applied to the post-filter set,
        not the pre-filter set — so the caller can never get more items than
        would pass the threshold."""
        strong = [
            await memory.store_episode(
                summary=f"quantum entanglement experiment session {i}",
                context={"session": i},
                importance=0.7,
            )
            for i in range(2)
        ]
        for i in range(4):
            await memory.store_episode(
                summary=f"quantum chemistry lecture {i}",
                context={"lecture": i},
                importance=0.7,
            )
        # min_score=0.0: no filter, limit=3 should return exactly 3.
        results = await memory.recall("quantum entanglement", limit=3, min_score=0.0)
        assert len(results) == 3

        # min_score=0.9: the four weak rows go before the limit, leaving two.
        results = await memory.recall("quantum entanglement", limit=3, min_score=0.9)
        assert sorted(ep.id for ep in results) == sorted(strong)

    async def test_high_min_score_keeps_only_the_strong_match(self, memory: EpisodicMemory):
        """A high min_score keeps the strong match and drops the weak ones.

        This checks that the SQL floor is actually wired: the target shares
        both query words, the ten distractors only "payment", so at 0.95 of
        the best relevance only the target remains.
        """
        target = await memory.store_episode(
            summary="Reviewed pull request for payment gateway integration",
            context={"pr": 77},
            importance=0.8,
        )
        for i in range(10):
            await memory.store_episode(
                summary=f"Other unrelated payment processing notes session {i}",
                context={"i": i},
                importance=0.5,
            )

        high_threshold_results = await memory.recall(
            "payment gateway", min_score=0.95
        )
        # 0.0 threshold: admits every match, up to the default limit of 10.
        all_results = await memory.recall("payment gateway", min_score=0.0)

        assert [ep.id for ep in high_threshold_results] == [target]
        assert len(all_results) == 10
        assert all_results[0].id == target

    async def test_min_score_empty_query_ignores_threshold(self, memory: EpisodicMemory):
        """Empty query uses recency path (no FTS5); min_score is ignored."""
        for i in range(3):
            await memory.store_episode(summary=f"Episode {i}", context={})
        # recency path doesn't apply BM25 scoring, so min_score=1.0 still
        # returns all items (threshold is irrelevant without FTS5 scores).
        results = await memory.recall("", min_score=1.0, limit=10)
        assert len(results) == 3

    async def test_min_score_like_fallback_ignores_threshold(self):
        """LIKE-fallback path returns same results regardless of min_score."""
        mem = EpisodicMemory(agent_id="test-like-min", db_path=":memory:")
        with patch("agents.memory.episodic._fts5_available", new_callable=AsyncMock) as mock_fts5:
            mock_fts5.return_value = False
            await mem.initialize()

        await mem.store_episode(
            summary="yttrium alloy synthesis protocol",
            context={},
        )
        results_none = await mem.recall("yttrium alloy", min_score=None)
        results_zero = await mem.recall("yttrium alloy", min_score=0.0)
        results_one = await mem.recall("yttrium alloy", min_score=1.0)

        # LIKE fallback: all three return the same episode since LIKE matches score 1.0.
        assert len(results_none) == 1
        assert len(results_zero) == 1
        assert len(results_one) == 1

        await mem.close()


# ─── recall_notes(min_score=...) — FTS5 filtering (RFC 0017 §C) ──


class TestRecallNotesMinScore:
    """Tests for the min_score parameter on EpisodicMemory.recall_notes().

    Mirrors TestRecallMinScore for the notes tier.
    """

    async def test_min_score_none_same_as_default(self, memory: EpisodicMemory):
        """min_score=None behaves identically to omitting the parameter."""
        await memory.store_note("molybdenum", "Molybdenum alloy processing notes")
        results_default = await memory.recall_notes("molybdenum alloy")
        results_none = await memory.recall_notes("molybdenum alloy", min_score=None)
        assert len(results_none) == len(results_default)
        assert {n.id for n in results_none} == {n.id for n in results_default}

    async def test_min_score_zero_admits_all_fts5_note_matches(self, memory: EpisodicMemory):
        """min_score=0.0 passes every FTS5 note match."""
        for i in range(3):
            await memory.store_note(
                f"palladium-{i}",
                f"palladium catalyst synthesis step {i}",
            )
        results = await memory.recall_notes("palladium catalyst", min_score=0.0)
        assert len(results) == 3

    async def test_min_score_one_keeps_only_the_best_note(self, memory: EpisodicMemory):
        """min_score=1.0 keeps the best note and drops the weaker one."""
        target = await memory.store_note(
            "osmium processing",
            "osmium isotope separation via centrifuge",
        )
        await memory.store_note("osmium prices", "osmium price sheet")
        await memory.store_note("suppliers", "iridium supplier list")
        results = await memory.recall_notes("osmium isotope", min_score=1.0)
        assert [n.id for n in results] == [target]
        assert len(await memory.recall_notes("osmium isotope", min_score=0.0)) == 2

    async def test_min_score_like_fallback_notes_ignores_threshold(self):
        """LIKE fallback for notes treats all matches as score 1.0."""
        mem = EpisodicMemory(agent_id="test-notes-like", db_path=":memory:")
        with patch("agents.memory.episodic._fts5_available", new_callable=AsyncMock) as mock_fts5:
            mock_fts5.return_value = False
            await mem.initialize()

        await mem.store_note("rhenium", "rhenium superalloy turbine blade coating")
        results_none = await mem.recall_notes("rhenium superalloy", min_score=None)
        results_one = await mem.recall_notes("rhenium superalloy", min_score=1.0)

        assert len(results_none) == 1
        assert len(results_one) == 1

        await mem.close()

    async def test_min_score_empty_notes_query_ignores_threshold(self, memory: EpisodicMemory):
        """Empty query uses recency path; min_score has no effect."""
        for i in range(2):
            await memory.store_note(f"topic-{i}", f"content {i}")
        results = await memory.recall_notes("", min_score=1.0, limit=10)
        assert len(results) == 2


# ─── PR 6 — RFC 0017 review follow-ups ───────────────────────────────────────


class TestRecallNotesMinScoreValidation:
    """PR 6 — RFC 0017 PR 3 review finding 1.

    Mirror the ``recall()`` ``min_score`` range guard at the public façade
    so the validation survives a future ``NoteStore`` refactor that drops
    the inner check.
    """

    async def test_recall_notes_negative_min_score_raises(
        self, memory: EpisodicMemory
    ):
        with pytest.raises(ValueError, match="min_score must be in"):
            await memory.recall_notes("anything", min_score=-0.1)

    async def test_recall_notes_above_one_min_score_raises(
        self, memory: EpisodicMemory
    ):
        with pytest.raises(ValueError, match="min_score must be in"):
            await memory.recall_notes("anything", min_score=1.5)

    async def test_recall_notes_none_min_score_accepted(
        self, memory: EpisodicMemory
    ):
        # Should not raise — no result assertion needed; this is a guard test.
        await memory.recall_notes("anything", min_score=None)

    async def test_recall_notes_zero_and_one_boundaries_accepted(
        self, memory: EpisodicMemory
    ):
        await memory.recall_notes("anything", min_score=0.0)
        await memory.recall_notes("anything", min_score=1.0)


class TestResolveMinScoreHelper:
    """PR 6 — RFC 0017 PR 3 review finding 3.

    The ``resolve_min_score`` helper centralises ``None → 0.0`` semantics
    shared by ``recall_fts5`` and ``NoteStore._recall_notes_fts5``.
    Renamed from ``_resolve_min_score`` in PR 6 review follow-ups
    because it is imported cross-module by ``notes.py`` in production
    code (see ``episodic_queries.__all__`` rationale).
    """

    def test_none_resolves_to_zero(self):
        from agents.memory.episodic_queries import resolve_min_score
        assert resolve_min_score(None) == 0.0

    def test_zero_resolves_to_zero(self):
        from agents.memory.episodic_queries import resolve_min_score
        assert resolve_min_score(0.0) == 0.0

    def test_explicit_value_passes_through(self):
        from agents.memory.episodic_queries import resolve_min_score
        assert resolve_min_score(0.42) == 0.42


class TestRecallMinScoreNoneZeroEquivalence:
    """PR 6 — RFC 0017 PR 3 review finding 4.

    ``min_score=None`` and ``min_score=0.0`` must produce identical SQL
    behaviour.  Pin the contract so a future change to the helper from
    finding 3 cannot silently introduce a non-zero implicit floor.
    """

    async def test_recall_none_and_zero_return_identical_results(
        self, memory: EpisodicMemory
    ):
        for i in range(4):
            await memory.store_episode(
                summary=f"helium ionisation experiment session {i}",
                context={"i": i},
                importance=0.5,
            )
        none_results = await memory.recall("helium ionisation", min_score=None)
        zero_results = await memory.recall("helium ionisation", min_score=0.0)

        assert [ep.id for ep in none_results] == [ep.id for ep in zero_results]


class TestHasFTS5Property:
    """PR 6 — RFC 0017 PR 4 review finding 3.

    Public ``has_fts5`` property replaces direct ``_fts5`` access.
    """

    async def test_has_fts5_property_returns_internal_flag(
        self, memory: EpisodicMemory
    ):
        # The fixture initialises memory; the property must reflect the
        # internal flag exactly.
        assert memory.has_fts5 == memory._fts5  # type: ignore[attr-defined]

    async def test_has_fts5_is_read_only(self, memory: EpisodicMemory):
        """The property has no setter — assignment must raise AttributeError."""
        with pytest.raises(AttributeError):
            memory.has_fts5 = False  # type: ignore[misc]
