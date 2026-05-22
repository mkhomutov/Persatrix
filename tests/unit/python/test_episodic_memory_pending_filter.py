"""
Regression test for the RFC 0020 PR 5 "summary pending" recall filter.

PR 5 originally placed the ``[summary pending]`` skip filter inside
:func:`MemoryStore.retrieve_relevant`. PR-262 deep-review finding **M1**
identified that the persona prompt-assembly path
(:mod:`agents.persona_runtime.memory_context`) calls
``EpisodicMemory.recall`` directly, bypassing the facade, and therefore
could surface the placeholder text in the LLM prompt during the
two-phase close path's race window (sync ``INSERT`` of
:data:`SUMMARY_PENDING_TEXT` → async LLM summariser → ``UPDATE``).

The fix lifts the filter to :meth:`EpisodicMemory.recall` so every
caller — facade, persona prompt assembly, shared-pool — is protected by
a single chokepoint. The filter is intentionally narrow: it drops only
the exact :data:`SUMMARY_PENDING_TEXT` sentinel, never the
:data:`SUMMARY_UNAVAILABLE_TEXT` janitor fallback (which the operator
explicitly published when summarisation failed and which must remain
visible).
"""

from __future__ import annotations

from agents.memory.episodic import EpisodicMemory
from agents.memory.interactions import (
    SUMMARY_PENDING_TEXT,
    SUMMARY_UNAVAILABLE_TEXT,
)


class TestRecallHidesSummaryPendingRows:
    """RFC 0020 PR 5 / PR-262 review M1: defense-in-depth at recall."""

    async def test_recall_recency_drops_pending_row(
        self, memory: EpisodicMemory,
    ) -> None:
        """Empty-query recency path must skip pending-summary rows."""
        await memory.store_episode(
            summary="alpha closed",
            context={"scope": "thread:t-1"},
            importance=0.8,
            interaction_id="alpha",
            started_at=100.0,
            closed_at=110.0,
            turn_count=3,
            scope="thread:t-1",
        )
        await memory.store_episode(
            summary=SUMMARY_PENDING_TEXT,
            context={"scope": "thread:t-2"},
            importance=0.8,
            interaction_id="beta",
            started_at=200.0,
            closed_at=210.0,
            turn_count=4,
            scope="thread:t-2",
        )

        # Empty query → recency path. Without the filter both rows surface.
        results = await memory.recall("", limit=10)
        summaries = {ep.summary for ep in results}
        assert "alpha closed" in summaries
        assert SUMMARY_PENDING_TEXT not in summaries

    async def test_recall_query_path_drops_pending_row(
        self, memory: EpisodicMemory,
    ) -> None:
        """Query (FTS5 / LIKE) path must also skip pending-summary rows.

        The placeholder text contains ``"summary"`` and ``"pending"`` —
        common enough tokens that a query like ``"pending status"`` could
        legitimately retrieve the placeholder via FTS5/LIKE. The filter
        runs after recall ranking, so the placeholder is dropped
        regardless of whether the ranker considered it a match.
        """
        await memory.store_episode(
            summary="pending status review for Q3 budget",
            context={"scope": "thread:t-real"},
            importance=0.8,
            scope="thread:t-real",
        )
        await memory.store_episode(
            summary=SUMMARY_PENDING_TEXT,
            context={"scope": "thread:t-pending"},
            importance=0.8,
            interaction_id="beta",
            started_at=200.0,
            closed_at=210.0,
            turn_count=4,
            scope="thread:t-pending",
        )

        results = await memory.recall("pending status", limit=10)
        summaries = {ep.summary for ep in results}
        # Real review row passes through; placeholder is dropped.
        assert "pending status review for Q3 budget" in summaries
        assert SUMMARY_PENDING_TEXT not in summaries

    async def test_unavailable_fallback_is_preserved(
        self, memory: EpisodicMemory,
    ) -> None:
        """The janitor's ``[interaction summary unavailable]`` fallback
        must keep surfacing — it is the operator's signal that
        summarisation failed; hiding it would silently swallow real
        conversational data the agent participated in.
        """
        await memory.store_episode(
            summary=SUMMARY_UNAVAILABLE_TEXT,
            context={"scope": "thread:t-3"},
            importance=0.8,
            interaction_id="gamma",
            started_at=300.0,
            closed_at=310.0,
            turn_count=5,
            scope="thread:t-3",
        )

        results = await memory.recall("", limit=10)
        summaries = {ep.summary for ep in results}
        assert SUMMARY_UNAVAILABLE_TEXT in summaries

    async def test_filter_reduces_visible_count_not_recall_limit(
        self, memory: EpisodicMemory,
    ) -> None:
        """The filter is a post-recall skip — when every recalled row is
        a pending placeholder, recall returns an empty list rather than
        re-querying for more rows. This pins the cheap-and-correct
        behaviour: a pending placeholder is rare (only emitted during
        the seconds-long LLM-summariser race window), so paying a
        re-query cost to backfill the limit would be optimising for an
        edge case.
        """
        for idx in range(3):
            await memory.store_episode(
                summary=SUMMARY_PENDING_TEXT,
                context={"scope": f"thread:t-{idx}"},
                importance=0.8,
                interaction_id=f"pending-{idx}",
                started_at=100.0 + idx,
                closed_at=110.0 + idx,
                turn_count=2,
                scope=f"thread:t-{idx}",
            )

        results = await memory.recall("", limit=10)
        assert results == []


class TestUpdateEpisodeSummaryAgentScoping:
    """PR 6 review #28 — ``update_episode_summary`` is agent-scoped.

    Two agents sharing a DB (the persona-orchestrator multi-tenant
    shape) must not be able to update each other's pending rows.
    Pins the agent-scoped ``WHERE`` clause so a future refactor
    cannot regress to a global UPDATE that crosses agent boundaries.
    """

    async def test_update_does_not_touch_other_agents_row(
        self, memory_pair: tuple[EpisodicMemory, EpisodicMemory],
    ) -> None:
        mem_a, mem_b = memory_pair
        # Both agents write a pending row under the same interaction_id —
        # the scoped UPDATE must match exactly one row (agent A's).
        shared_iid = "shared-iid"
        await mem_a.store_episode(
            summary=SUMMARY_PENDING_TEXT, context={}, importance=0.8,
            interaction_id=shared_iid, started_at=100.0, closed_at=110.0,
            turn_count=3, scope="dm:peer:agent-a",
        )
        await mem_b.store_episode(
            summary=SUMMARY_PENDING_TEXT, context={}, importance=0.8,
            interaction_id=shared_iid, started_at=200.0, closed_at=210.0,
            turn_count=3, scope="dm:peer:agent-b",
        )

        updated = await mem_a.update_episode_summary(
            shared_iid, "agent-a's real summary",
        )
        assert updated is True

        # Agent A's row updated; agent B's row still on the sentinel.
        db_a = mem_a._ensure_db()
        async with db_a.execute(
            "SELECT agent_id, summary FROM episodes "
            "WHERE interaction_id = ? ORDER BY agent_id",
            (shared_iid,),
        ) as cursor:
            rows = await cursor.fetchall()
        assert rows == [
            ("agent-a", "agent-a's real summary"),
            ("agent-b", SUMMARY_PENDING_TEXT),
        ]
