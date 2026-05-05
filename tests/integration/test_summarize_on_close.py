"""
RFC 0020 PR 4 — summarisation-on-close + janitor + record_interaction
move integration tests.

Pins the PR 4 deliverables called out in
``docs/rfcs/0020-pr-plan.md`` §PR 4:

* Multi-turn close runs the LLM summariser through
  :func:`MemoryFacade.compress` and writes the LLM text to the episode
  ``summary`` column.
* LLM failure / timeout / empty-text paths fall back to
  :data:`SUMMARY_UNAVAILABLE_TEXT` and increment the
  ``agent.interactions.summary.failed`` counter.
* ``record_interaction`` is called exactly once per closed interaction
  (RFC 0020 §F) — moved out of the per-event chat handler.
* The auto-reflect counter increments per closed interaction (RFC 0020
  §H), not per inbound event.
* The janitor :func:`cleanup_closing_interactions` backfills rows
  stuck on ``[summary pending]`` past the grace window.
"""

from __future__ import annotations

import time

import pytest

from agents.memory.interactions import (
    SUMMARY_PENDING_TEXT,
    SUMMARY_UNAVAILABLE_TEXT,
)
from agents.persona_types import AgentEvent, EventType
from agents.tools.registry import clear_registry

from ._summarize_close_helpers import (
    LLM_SUMMARY_TEXT,
    drain,
    episode_summary,
    make_agent,
    make_failing_summary_client,
    send_n_turns,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


# ─── Multi-turn LLM summary ─────────────────────────────────


@pytest.mark.asyncio
class TestSummarisationOnClose:
    """RFC 0020 PR 4 §"Summarisation hook"."""

    async def test_ten_turn_session_uses_llm_summary(self):
        """A 10-turn DM closes with the LLM-generated summary text."""
        agent = await make_agent()
        peer = "iron-fox"
        await send_n_turns(agent, peer, 10)
        await agent.on_event(AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "thanks, bye"},
            sender_id=peer,
            metadata={"chat_end": True},
        ))
        await drain(agent)

        summary = await episode_summary(agent)
        # Non-fallback, non-placeholder, equal to mock text.
        assert summary == LLM_SUMMARY_TEXT
        assert summary != SUMMARY_UNAVAILABLE_TEXT
        assert summary != SUMMARY_PENDING_TEXT

    async def test_llm_failure_falls_back_and_persists_episode(self):
        """LLM exception path: episode persists with fallback text."""
        agent = await make_agent(
            client=make_failing_summary_client(RuntimeError("boom")),
        )
        peer = "iron-fox"
        await send_n_turns(agent, peer, 4)
        await agent.on_event(AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "bye"},
            sender_id=peer,
            metadata={"chat_end": True},
        ))
        await drain(agent)

        summary = await episode_summary(agent)
        assert summary == SUMMARY_UNAVAILABLE_TEXT


# ─── record_interaction relocation (RFC 0020 §F) ─────────


@pytest.mark.asyncio
class TestRecordInteractionMove:
    """``record_interaction`` is called once per closed interaction."""

    async def test_record_interaction_fires_once_not_per_turn(self):
        """11 inbound turns → 1 relationship-row bump."""
        agent = await make_agent()
        peer = "iron-fox"
        await send_n_turns(agent, peer, 10)
        await agent.on_event(AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "thanks, bye"},
            sender_id=peer,
            metadata={"chat_end": True},
        ))
        await drain(agent)

        rel = await agent._relationship_memory.get_relationship_summary(peer)
        assert rel is not None
        # Pre-PR-4 the per-event handler bumped this 11 times; post-PR-4
        # only the close-path call fires, so ``interaction_count`` is 1.
        assert rel.interaction_count == 1


# ─── Auto-reflect counter (RFC 0020 §H) ──────────────────


@pytest.mark.asyncio
class TestAutoReflectCounter:
    """Counter ticks per closed interaction, not per inbound event."""

    async def test_counter_increments_once_per_closed_interaction(self):
        agent = await make_agent()
        peer = "iron-fox"

        before = await agent._episodic_memory.get_interaction_count()
        await send_n_turns(agent, peer, 5)
        await agent.on_event(AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "bye"},
            sender_id=peer,
            metadata={"chat_end": True},
        ))
        await drain(agent)
        after = await agent._episodic_memory.get_interaction_count()

        # 6 inbound events, 1 closed interaction → counter delta == 1.
        assert after - before == 1


# ─── Janitor (RFC 0020 §C) ───────────────────────────────


@pytest.mark.asyncio
class TestJanitorBackfillsPendingSummaries:
    """``cleanup_closing_interactions`` upgrades stuck rows to the fallback."""

    async def test_pending_row_past_grace_is_backfilled(self):
        agent = await make_agent()

        # Insert a row that mimics a crashed close-path: ``[summary pending]``
        # text with a ``closed_at`` older than the grace window.
        db = agent._episodic_memory._ensure_db()
        old_closed_at = time.time() - 10_000.0
        await db.execute(
            """
            INSERT INTO episodes
                (agent_id, summary, context_json, created_at,
                 interaction_id, started_at, closed_at, turn_count, scope)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                agent.agent_id,
                SUMMARY_PENDING_TEXT,
                "{}",
                old_closed_at,
                "stuck-interaction-id",
                old_closed_at - 60.0,
                old_closed_at,
                3,
                "dm:agent:peer",
            ),
        )
        await db.commit()

        # grace_sec=1.0 — the inserted row's closed_at is well past it.
        upgraded = await agent.cleanup_closing_interactions(grace_sec=1.0)
        assert upgraded == 1

        async with db.execute(
            "SELECT summary FROM episodes WHERE interaction_id = ?",
            ("stuck-interaction-id",),
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] == SUMMARY_UNAVAILABLE_TEXT

    async def test_recent_pending_row_is_left_alone(self):
        """Within the grace window the janitor does not touch the row."""
        agent = await make_agent()
        db = agent._episodic_memory._ensure_db()
        recent_closed_at = time.time() - 0.5
        await db.execute(
            """
            INSERT INTO episodes
                (agent_id, summary, context_json, created_at,
                 interaction_id, started_at, closed_at, turn_count, scope)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                agent.agent_id,
                SUMMARY_PENDING_TEXT,
                "{}",
                recent_closed_at,
                "recent-interaction-id",
                recent_closed_at - 60.0,
                recent_closed_at,
                3,
                "dm:agent:peer",
            ),
        )
        await db.commit()

        upgraded = await agent.cleanup_closing_interactions(grace_sec=300.0)
        assert upgraded == 0

