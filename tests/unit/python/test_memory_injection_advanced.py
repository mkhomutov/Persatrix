"""Tests for _inject_memory_context: truncation, stale-section clearing,
and user/agent relationship lookup."""

from unittest.mock import patch

from agents.persona import create_persona_agent
from agents.persona_types import AgentEvent, EventType

from ._persona_test_helpers import _PERSONA_CONFIG, _make_client

# ─── F-5b-1: _inject_memory_context (truncation + stale state) ──


class TestInjectMemoryContextAdvanced:
    """Truncation, stale-section clearing, and participant-type lookup tests
    for _inject_memory_context.  Counterpart to TestInjectMemoryContext."""

    async def test_relationship_notes_truncated(self):
        """F-60-5: relationship notes exceeding 300 chars are truncated."""
        agent = create_persona_agent(
            agent_id="ember-owl", config=_PERSONA_CONFIG, llm_client=_make_client(),
        )
        await agent.initialize_memory()

        # Record interaction to create a relationship with long notes.
        await agent._relationship_memory.record_interaction(
            other_id="iron-fox",
            interaction_type="collaboration",
            outcome="success",
            sentiment=0.8,
        )
        # Manually set long notes on the relationship.
        # NOTE: RelationshipMemory has no public setter for the `notes`
        # column (it accumulates notes through record_interaction() which
        # does not directly expose the notes field).  Using the raw DB
        # connection is the only way to inject a controlled long string
        # without adding a test-only API to production code.  If the
        # `relationships` table schema changes (e.g. column rename), this
        # raw execute will fail with an sqlite3.OperationalError rather
        # than an assertion error — treat that as a reminder to update
        # the fixture.  (PR review: coupling note for future maintainers.)
        long_notes = "n" * 600
        # Include all composite PK columns in WHERE to be resilient
        # against fixtures with both agent and user relationships
        # sharing the same participant IDs.
        # (PR #120 review F-8: incomplete WHERE clause.)
        async with agent._relationship_memory._db.execute(
            "UPDATE relationships SET notes = ? "
            "WHERE participant_id = ? AND participant_type = 'agent' "
            "AND other_participant_id = ? AND other_participant_type = 'agent'",
            (long_notes, "ember-owl", "iron-fox"),
        ):
            pass
        await agent._relationship_memory._db.commit()

        event = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "Hello"},
            sender_id="iron-fox",
        )
        await agent._inject_memory_context(event)

        rel_section = agent._working_memory.get_section("relationship_context")
        assert rel_section is not None
        # Full 600-char notes should NOT appear — capped at 300.
        assert long_notes not in rel_section.content
        assert "n" * 300 in rel_section.content
        await agent.close_memory()

    async def test_episode_summary_truncated_with_ellipsis(self):
        """F-60-R2-3: episode summaries exceeding 200 chars get word-boundary
        truncation with trailing '...' so the LLM knows text was truncated.
        """
        agent = create_persona_agent(
            agent_id="ember-owl", config=_PERSONA_CONFIG, llm_client=_make_client(),
        )
        await agent.initialize_memory()

        # Store an episode with a long summary (>200 chars).  ``public``
        # because a TASK_ASSIGNED turn takes the RFC 0037 §D acting floor
        # (rule (b)); this test's subject is truncation, not the gate.
        long_summary = "architecture " * 20  # 260 chars
        await agent._episodic_memory.store_episode(
            summary=long_summary,
            context={"topic": "arch"},
            importance=0.8,
            protection_level="public",
        )

        event = AgentEvent(
            event_type=EventType.TASK_ASSIGNED,
            payload={"task": "architecture"},
        )
        with patch.object(agent, "_format_event", return_value="architecture"):
            await agent._inject_memory_context(event)

        section = agent._working_memory.get_section("episodic_recall")
        assert section is not None
        # Should end with "..." and NOT contain the full summary.
        assert section.content.endswith("...")
        assert long_summary not in section.content
        await agent.close_memory()

    async def test_note_content_truncated_with_ellipsis(self):
        """F-60-R2-3: note content exceeding 500 chars gets word-boundary
        truncation with trailing '...' so the LLM knows text was truncated.
        """
        agent = create_persona_agent(
            agent_id="ember-owl", config=_PERSONA_CONFIG, llm_client=_make_client(),
        )
        await agent.initialize_memory()

        long_content = "word " * 120  # 600 chars
        # ``public`` — the TASK_ASSIGNED turn takes the §D acting floor.
        await agent._episodic_memory.store_note(
            topic="verbose",
            content=long_content,
            protection_level="public",
        )

        event = AgentEvent(
            event_type=EventType.TASK_ASSIGNED,
            payload={"task": "verbose"},
        )
        with patch.object(agent, "_format_event", return_value="verbose"):
            await agent._inject_memory_context(event)

        section = agent._working_memory.get_section("recent_notes")
        assert section is not None
        # Should end with "..." and NOT contain the full content.
        assert "..." in section.content
        assert long_content not in section.content
        await agent.close_memory()

    async def test_memory_context_reaches_llm_prompt(self):
        """F-60-R2-1 + F-60-R2-8: verify working memory context appears in
        the system prompt sent to the LLM.

        _inject_memory_context() adds sections to working memory, and
        _on_event_inner() calls build_context() to include them in the
        system prompt.  This end-to-end test captures the system= kwarg
        from the LLM call and asserts the note context is present.
        """
        client = _make_client()
        agent = create_persona_agent(
            agent_id="ember-owl", config=_PERSONA_CONFIG, llm_client=client,
        )
        await agent.initialize_memory()

        # Store a note so inject_memory_context has content to add.
        # ``public`` — the TASK_ASSIGNED turn takes the §D acting floor.
        await agent._episodic_memory.store_note(
            topic="architecture",
            content="Consider event sourcing for the migration",
            protection_level="public",
        )

        event = AgentEvent(
            event_type=EventType.TASK_ASSIGNED,
            payload={"task": "architecture review"},
        )
        # Patch _format_event to return a simple query matchable by LIKE
        # fallback (FTS5 unavailable on :memory: SQLite).
        with patch.object(agent, "_format_event", return_value="architecture"):
            async with agent._lock:
                await agent._on_event_inner(event)

        # Capture the system= kwarg passed to the LLM provider.
        call_kwargs = client._provider.create_message.call_args
        system_prompt = call_kwargs.kwargs.get("system", "")

        # The note content should appear in the system prompt via
        # build_context() → memory_sections append.
        assert "event sourcing" in system_prompt.lower(), (
            f"Expected note context in system prompt, got: {system_prompt[:500]}"
        )
        await agent.close_memory()

    async def test_episode_summary_no_space_still_gets_ellipsis(self):
        """PR review should-fix #3: zero-space truncation edge case.

        A 260-char episode summary with no spaces (e.g. a UUID or hash
        run) should still get '...' appended, not silently omit it.
        The word-boundary guard falls back to the full 200-char slice
        when there is no space, and '...' is appended regardless.
        """
        agent = create_persona_agent(
            agent_id="ember-owl", config=_PERSONA_CONFIG, llm_client=_make_client(),
        )
        await agent.initialize_memory()

        # 260 chars, no spaces — simulates a hash chain or URL path.
        # ``public`` — the TASK_ASSIGNED turn takes the §D acting floor.
        no_space_summary = "a" * 260
        await agent._episodic_memory.store_episode(
            summary=no_space_summary,
            context={"topic": "aaaa"},
            importance=0.8,
            protection_level="public",
        )

        event = AgentEvent(
            event_type=EventType.TASK_ASSIGNED,
            payload={"task": "aaaa"},
        )
        with patch.object(agent, "_format_event", return_value="aaaa"):
            await agent._inject_memory_context(event)

        section = agent._working_memory.get_section("episodic_recall")
        assert section is not None
        # '...' must appear even when there are no word boundaries.
        assert "..." in section.content
        # The full 260-char string must not appear (it was truncated).
        assert no_space_summary not in section.content
        await agent.close_memory()

    async def test_stale_relationship_cleared_on_sender_less_event(self):
        """PR review must-fix #1: stale relationship_context is removed
        when a sender-less event (e.g. TICK) follows a sender event.

        Without the remove_section() guard, alice's relationship context
        would persist in working memory and silently influence the LLM
        response for the subsequent TICK event.
        """
        agent = create_persona_agent(
            agent_id="ember-owl", config=_PERSONA_CONFIG, llm_client=_make_client(),
        )
        await agent.initialize_memory()

        # Record an interaction so there is a relationship to inject.
        await agent._relationship_memory.record_interaction(
            other_id="alice",
            interaction_type="collaboration",
            outcome="success",
            sentiment=0.9,
        )

        # Event 1: message from alice — relationship section added.
        sender_event = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "Hello"},
            sender_id="alice",
        )
        await agent._inject_memory_context(sender_event)
        assert agent._working_memory.get_section("relationship_context") is not None

        # Event 2: TICK (no sender) — relationship section must be cleared.
        tick_event = AgentEvent(
            event_type=EventType.TICK,
            payload={},
        )
        await agent._inject_memory_context(tick_event)
        assert agent._working_memory.get_section("relationship_context") is None, (
            "Stale relationship_context from alice persisted into TICK event"
        )
        await agent.close_memory()

    async def test_stale_episodic_cleared_on_tick(self):
        """F-60-R1/R2: stale episodic_recall section from event N is
        removed when event N+1 finds no episodes (e.g. TICK bypass).

        test_stale_relationship_cleared_on_sender_less_event covers the
        relationship_context tier with the same scenario.  This test
        covers the episodic tier, which previously lacked the upfront
        remove_section() guard (finding F-60-R1).
        """
        agent = create_persona_agent(
            agent_id="ember-owl", config=_PERSONA_CONFIG, llm_client=_make_client(),
        )
        await agent.initialize_memory()

        # ``public`` — the unclassified CHANNEL_MESSAGE below floors to
        # ``public`` per §A rule (b) (the version-skew posture).
        await agent._episodic_memory.store_episode(
            summary="Architecture discussion with the team",
            context={},
            importance=0.8,
            protection_level="public",
        )

        # Event 1: MESSAGE — FTS5 finds the episode; episodic_recall added.
        msg_event = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "architecture"},
        )
        with patch.object(agent, "_format_event", return_value="architecture"):
            await agent._inject_memory_context(msg_event)
        assert agent._working_memory.get_section("episodic_recall") is not None, (
            "Expected episodic_recall section after MESSAGE event"
        )

        # Event 2: TICK — episodic recall is skipped entirely for TICK events.
        # The stale section from event 1 must be cleared before add_section()
        # would have been called (which it isn't, because TICK bypasses recall).
        tick_event = AgentEvent(
            event_type=EventType.TICK,
            payload={},
        )
        await agent._inject_memory_context(tick_event)
        assert agent._working_memory.get_section("episodic_recall") is None, (
            "Stale episodic_recall from MESSAGE event persisted into TICK event"
        )
        await agent.close_memory()

    async def test_stale_notes_cleared_when_no_notes_exist(self):
        """F-60-R1: stale recent_notes section is removed when the next
        event finds no notes at all (neither FTS5 match nor recency).

        Without the upfront remove_section() guard, notes from event N
        would persist as recent_notes and reach the LLM for event N+1.
        """
        agent = create_persona_agent(
            agent_id="ember-owl", config=_PERSONA_CONFIG, llm_client=_make_client(),
        )
        await agent.initialize_memory()

        # ``public`` — the TASK_ASSIGNED turn takes the §D acting floor.
        await agent._episodic_memory.store_note(
            topic="architecture",
            content="Consider event sourcing for architecture scalability",
            protection_level="public",
        )

        # Event 1: query matches the note — recent_notes section added.
        event1 = AgentEvent(
            event_type=EventType.TASK_ASSIGNED,
            payload={"task": "architecture"},
        )
        with patch.object(agent, "_format_event", return_value="architecture"):
            await agent._inject_memory_context(event1)
        assert agent._working_memory.get_section("recent_notes") is not None, (
            "Expected recent_notes after first event"
        )

        # Delete all notes so neither FTS5 nor recency fallback can find any.
        notes = await agent._episodic_memory.recall_notes("", limit=100)
        for note in notes:
            await agent._episodic_memory.delete_note(note.id)

        # Event 2: no notes remain in DB — section must be cleared.
        event2 = AgentEvent(
            event_type=EventType.TASK_ASSIGNED,
            payload={"task": "zzz-no-match-zzz"},
        )
        with patch.object(agent, "_format_event", return_value="zzz-no-match-zzz"):
            await agent._inject_memory_context(event2)
        assert agent._working_memory.get_section("recent_notes") is None, (
            "Stale recent_notes from event 1 persisted into event 2"
        )
        await agent.close_memory()

    async def test_user_relationship_injected_via_metadata(self):
        """PR #120 review F-1/F-9: user→agent event flow injects user
        relationship correctly.

        Validates the full path:
        1. Agent records an interaction with other_participant_type="user"
        2. A user sends CHANNEL_MESSAGE with sender_participant_type="user"
        3. _inject_memory_context() queries the user relationship
        4. The relationship section labels the sender as "(Human user)"

        This is the integration path where F-1 (missing other_participant_type
        propagation) would manifest as a silently missing relationship.
        """
        agent = create_persona_agent(
            agent_id="ember-owl", config=_PERSONA_CONFIG, llm_client=_make_client(),
        )
        await agent.initialize_memory()

        # Record a user interaction — note other_participant_type="user".
        await agent._relationship_memory.record_interaction(
            other_id="user-alice",
            interaction_type="conversation",
            outcome="positive",
            sentiment=0.7,
            other_participant_type="user",
        )

        # Simulate a user message with sender_participant_type in metadata.
        event = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "Hello from user"},
            sender_id="user-alice",
            metadata={"sender_participant_type": "user"},
        )
        await agent._inject_memory_context(event)

        rel_section = agent._working_memory.get_section("relationship_context")
        assert rel_section is not None, (
            "User relationship should have been injected into working memory"
        )
        assert "user-alice" in rel_section.content
        assert "(Human user)" in rel_section.content
        await agent.close_memory()

    async def test_agent_relationship_still_works_without_metadata(self):
        """PR #120 review F-1 regression guard: agent-to-agent relationships
        still work when no sender_participant_type metadata is present
        (backward compatibility with pre-generalization callers).
        """
        agent = create_persona_agent(
            agent_id="ember-owl", config=_PERSONA_CONFIG, llm_client=_make_client(),
        )
        await agent.initialize_memory()

        await agent._relationship_memory.record_interaction(
            other_id="iron-fox",
            interaction_type="collaboration",
            outcome="success",
            sentiment=0.8,
        )

        # No metadata at all — should default to "agent" lookup.
        event = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "Hello"},
            sender_id="iron-fox",
        )
        await agent._inject_memory_context(event)

        rel_section = agent._working_memory.get_section("relationship_context")
        assert rel_section is not None
        assert "iron-fox" in rel_section.content
        assert "(Human user)" not in rel_section.content
        await agent.close_memory()
