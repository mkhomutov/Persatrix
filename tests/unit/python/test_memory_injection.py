"""Tests for _inject_memory_context: core episodic, note, and relationship injection."""

from unittest.mock import AsyncMock, MagicMock, patch

from agents.persona import create_persona_agent
from agents.persona_types import AgentEvent, EventType

from ._persona_test_helpers import _PERSONA_CONFIG, _make_client


# ─── F-5b-1: _inject_memory_context (core behavior) ──────────


class TestInjectMemoryContext:
    """F-5b-1: Memory context injection into working memory — core behavior."""

    async def test_injects_episodic_and_notes(self):
        """_inject_memory_context adds episodic and note sections."""
        agent = create_persona_agent(
            agent_id="ember-owl", config=_PERSONA_CONFIG, llm_client=_make_client(),
        )
        await agent.initialize_memory()

        # Store an episode and a note so recall returns results.
        await agent._episodic_memory.store_episode(
            summary="Discussed architecture patterns",
            context={"topic": "arch"},
            importance=0.8,
        )
        await agent._episodic_memory.store_note(
            topic="architecture",
            content="Consider event sourcing for architecture",
        )

        event = AgentEvent(
            event_type=EventType.TASK_ASSIGNED,
            payload={"task": "architecture"},
        )
        # Patch _format_event to return a simple query that FTS5/LIKE can match.
        with patch.object(agent, "_format_event", return_value="architecture"):
            await agent._inject_memory_context(event)

        # Check that sections were added to working memory.
        episodic_section = agent._working_memory.get_section("episodic_recall")
        notes_section = agent._working_memory.get_section("recent_notes")
        assert episodic_section is not None
        assert "architecture" in episodic_section.content.lower()
        assert episodic_section.priority == 7
        assert notes_section is not None
        assert "event sourcing" in notes_section.content.lower()
        assert notes_section.priority == 6
        await agent.close_memory()

    async def test_injects_relationship_for_sender(self):
        """_inject_memory_context adds relationship section when sender known."""
        agent = create_persona_agent(
            agent_id="ember-owl", config=_PERSONA_CONFIG, llm_client=_make_client(),
        )
        await agent.initialize_memory()

        # Record an interaction to create a relationship.
        await agent._relationship_memory.record_interaction(
            other_id="iron-fox",
            interaction_type="collaboration",
            outcome="success",
            sentiment=0.8,
        )

        event = AgentEvent(
            event_type=EventType.MESSAGE_RECEIVED,
            payload={"content": "Hello"},
            sender_id="iron-fox",
        )
        await agent._inject_memory_context(event)

        rel_section = agent._working_memory.get_section("relationship_context")
        assert rel_section is not None
        assert "iron-fox" in rel_section.content
        assert rel_section.priority == 8
        await agent.close_memory()

    async def test_no_sender_skips_relationship(self):
        """_inject_memory_context skips relationship when no sender_id."""
        agent = create_persona_agent(
            agent_id="ember-owl", config=_PERSONA_CONFIG, llm_client=_make_client(),
        )
        await agent.initialize_memory()

        event = AgentEvent(
            event_type=EventType.TICK,
            payload={},
        )
        await agent._inject_memory_context(event)

        rel_section = agent._working_memory.get_section("relationship_context")
        assert rel_section is None
        await agent.close_memory()

    async def test_memory_error_graceful(self):
        """_inject_memory_context logs and continues if recall() raises."""
        agent = create_persona_agent(
            agent_id="ember-owl", config=_PERSONA_CONFIG, llm_client=_make_client(),
        )
        await agent.initialize_memory()

        # Sabotage recall to simulate failure.
        agent._episodic_memory.recall = AsyncMock(side_effect=RuntimeError("db locked"))
        agent._episodic_memory.recall_notes = AsyncMock(side_effect=RuntimeError("db locked"))

        event = AgentEvent(
            event_type=EventType.MESSAGE_RECEIVED,
            payload={"content": "test"},
            sender_id="iron-fox",
        )
        # Should not raise.
        await agent._inject_memory_context(event)

        # Episodic and notes sections should not be present.
        assert agent._working_memory.get_section("episodic_recall") is None
        assert agent._working_memory.get_section("recent_notes") is None
        await agent.close_memory()

    async def test_all_tiers_failing_still_proceeds(self):
        """_inject_memory_context handles all three memory tiers failing.

        Verifies that simultaneous failures across episodic recall,
        relationship lookup, and note recall are each caught independently
        and the method completes without raising.
        (PR #60 review: coverage gap — all-tiers-failing case.)
        """
        agent = create_persona_agent(
            agent_id="ember-owl", config=_PERSONA_CONFIG, llm_client=_make_client(),
        )
        await agent.initialize_memory()

        # Sabotage all three tiers.
        agent._episodic_memory.recall = AsyncMock(side_effect=OSError("disk full"))
        agent._episodic_memory.recall_notes = AsyncMock(side_effect=OSError("disk full"))
        agent._relationship_memory.get_relationship_summary = AsyncMock(
            side_effect=RuntimeError("corrupted index"),
        )

        event = AgentEvent(
            event_type=EventType.MESSAGE_RECEIVED,
            payload={"content": "test"},
            sender_id="iron-fox",
        )
        # Should not raise — all tiers fail gracefully.
        await agent._inject_memory_context(event)

        # No sections should be present.
        assert agent._working_memory.get_section("episodic_recall") is None
        assert agent._working_memory.get_section("relationship_context") is None
        assert agent._working_memory.get_section("recent_notes") is None
        await agent.close_memory()

    async def test_tick_calls_episodic_recall(self):
        """TICK events now call episodic recall (TICK skip removed in PR 4).

        RFC 0017 PR 4: the TICK skip at the top of _inject_memory_context is
        deleted.  Recall is now called for all event types; the min_score
        threshold filters low-signal results at the DB layer.  Notes recall
        is still attempted for all events.
        (Previously: PR #60 review TICK skip; removed in RFC 0017 PR 4.)
        """
        agent = create_persona_agent(
            agent_id="ember-owl", config=_PERSONA_CONFIG, llm_client=_make_client(),
        )
        await agent.initialize_memory()

        # Store an episode so recall would return results if called.
        await agent._episodic_memory.store_episode(
            summary="Previous architecture discussion",
            context={"topic": "arch"},
            importance=0.8,
        )

        # Spy on recall to verify it IS called for TICK (skip removed).
        recall_spy = AsyncMock(wraps=agent._episodic_memory.recall)
        agent._episodic_memory.recall = recall_spy

        # Spy on recall_notes to verify it IS still called.
        notes_spy = AsyncMock(wraps=agent._episodic_memory.recall_notes)
        agent._episodic_memory.recall_notes = notes_spy

        event = AgentEvent(event_type=EventType.TICK, payload={})
        await agent._inject_memory_context(event)

        # Episodic recall MUST be called for TICK events (skip removed).
        recall_spy.assert_called_once()
        # Notes recall is still attempted.
        assert notes_spy.call_count >= 1

        await agent.close_memory()

    async def test_zero_interaction_relationship_skips_injection(self):
        """Bootstrapped relationship with zero interactions skips injection.

        When a relationship is configured via YAML but no interactions have
        been recorded yet, ``interaction_count == 0`` and the relationship
        section is not injected.  This is intentional: a bootstrapped trust
        score without any interaction history provides no actionable context
        for the LLM.
        (PR #60 review: test zero-interaction relationship branch.)
        """
        agent = create_persona_agent(
            agent_id="ember-owl", config=_PERSONA_CONFIG, llm_client=_make_client(),
        )
        await agent.initialize_memory()

        # Bootstrap a relationship with trust but zero interactions.
        await agent._relationship_memory.update_trust(
            other_id="iron-fox",
            delta=0.1,
            reason="config bootstrap",
        )

        event = AgentEvent(
            event_type=EventType.MESSAGE_RECEIVED,
            payload={"content": "Hello"},
            sender_id="iron-fox",
        )
        await agent._inject_memory_context(event)

        # Relationship section should NOT be injected (zero interactions).
        rel_section = agent._working_memory.get_section("relationship_context")
        assert rel_section is None
        await agent.close_memory()

    async def test_note_content_truncated(self):
        """F-60-1: note content exceeding 500 chars is truncated.

        Notes can be up to 10KB (_MAX_NOTE_CONTENT_BYTES).  Injecting them
        without truncation wastes working memory budget and crowds out
        episodic and relationship context.
        """
        agent = create_persona_agent(
            agent_id="ember-owl", config=_PERSONA_CONFIG, llm_client=_make_client(),
        )
        await agent.initialize_memory()

        long_content = "x" * 1000
        await agent._episodic_memory.store_note(
            topic="verbose",
            content=long_content,
        )

        event = AgentEvent(
            event_type=EventType.TASK_ASSIGNED,
            payload={"task": "verbose"},
        )
        with patch.object(agent, "_format_event", return_value="verbose"):
            await agent._inject_memory_context(event)

        notes_section = agent._working_memory.get_section("recent_notes")
        assert notes_section is not None
        # The full 1000-char content should NOT appear — capped at 500.
        assert long_content not in notes_section.content
        assert "x" * 500 in notes_section.content
        await agent.close_memory()

    async def test_query_param_avoids_double_format_event(self):
        """F-60-2: passing query= skips internal _format_event() call.

        _on_event_inner() pre-computes user_message via _format_event()
        and passes it as query= to avoid a redundant call.
        """
        agent = create_persona_agent(
            agent_id="ember-owl", config=_PERSONA_CONFIG, llm_client=_make_client(),
        )
        await agent.initialize_memory()

        event = AgentEvent(
            event_type=EventType.TASK_ASSIGNED,
            payload={"task": "architecture review"},
        )

        spy = MagicMock(wraps=agent._format_event)
        agent._format_event = spy

        await agent._inject_memory_context(event, query="pre-computed query")

        # _format_event should NOT be called when query is provided.
        spy.assert_not_called()
        await agent.close_memory()

    async def test_default_trust_not_injected(self):
        """F-60-4: trust at default 0.5 is omitted from relationship context.

        A trust score of 0.50 provides no useful signal (it's just the
        initial value) and could mislead the LLM into thinking trust was
        measured.
        """
        agent = create_persona_agent(
            agent_id="ember-owl", config=_PERSONA_CONFIG, llm_client=_make_client(),
        )
        await agent.initialize_memory()

        # Record interaction without sentiment to keep trust at ~0.5.
        await agent._relationship_memory.record_interaction(
            other_id="iron-fox",
            interaction_type="collaboration",
            outcome="neutral",
            sentiment=0.0,
        )

        event = AgentEvent(
            event_type=EventType.MESSAGE_RECEIVED,
            payload={"content": "Hello"},
            sender_id="iron-fox",
        )
        await agent._inject_memory_context(event)

        rel_section = agent._working_memory.get_section("relationship_context")
        assert rel_section is not None
        # Trust line should NOT appear when trust is ~0.5 default.
        assert "Trust:" not in rel_section.content
        # Interaction count should still appear.
        assert "Interactions:" in rel_section.content
        await agent.close_memory()
