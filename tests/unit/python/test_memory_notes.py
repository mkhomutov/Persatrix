"""Tests for note injection behavior, memory namespace exposure, and
user-identity system prompt instructions."""

from unittest.mock import patch

from agents.persona import create_persona_agent
from agents.persona_types import AgentEvent, EventType

from ._persona_test_helpers import _PERSONA_CONFIG, _make_client

# ─── Note Recency Fallback ───────────────────────────────────


class TestNoteInjectionBehavior:
    """Tests for note injection behavior after RFC 0017 PR 4.

    PR 4 removes the ``should_fall_back`` recency-note fallback that was
    triggered by empty-notes + CHANNEL_MESSAGE + no-episodes.  The
    min_score threshold on recall/recall_notes is now the only filter;
    low-signal queries produce empty results and no section injection.

    Class renamed from ``TestNoteRecencyFallback`` in PR #148 (review
    finding N-1) so the name no longer references the deleted fallback.
    The assertions remain valid because the *outcomes* were unchanged —
    only the underlying *reason* (min_score filtering vs. fallback gate)
    shifted.  Individual test docstrings (see M-2 findings) were updated
    to explain the new mechanism.
    """

    async def test_low_signal_query_does_not_inject_notes(self):
        """Low-signal 'hi' query does not inject notes (fallback removed, PR 4).

        Previously, the should_fall_back path triggered recall_notes("", limit=3)
        when the user's message had no FTS5 overlap with note keywords.  PR 4
        removes that path; the min_score threshold is the sole filter.
        A 'hi' query with no min_score-passing matches produces no section.
        (Replaces: test_recency_fallback_injects_notes_on_keyword_miss.)
        """
        agent = create_persona_agent(
            agent_id="ember-owl", config=_PERSONA_CONFIG, llm_client=_make_client(),
        )
        await agent.initialize_memory()

        # Store notes with specific keywords that won't match "hi".
        await agent._episodic_memory.store_note(
            topic="Team Member - Max",
            content="Max is the creator of Persatrix",
        )

        event = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "hi"},
        )
        with patch.object(agent, "_format_event", return_value="hi"):
            await agent._inject_memory_context(event)

        # Fallback removed: low-signal 'hi' produces no section.
        section = agent._working_memory.get_section("recent_notes")
        assert section is None, (
            "Fallback removed in PR 4: low-signal 'hi' should not inject notes. "
            "min_score threshold is the only filter."
        )
        await agent.close_memory()

    async def test_recency_fallback_skipped_when_fts5_matches(self):
        """When FTS5 finds matching notes, recency fallback is not used."""
        agent = create_persona_agent(
            agent_id="ember-owl", config=_PERSONA_CONFIG, llm_client=_make_client(),
        )
        await agent.initialize_memory()

        # ``public`` — the TASK_ASSIGNED turn takes the RFC 0037 §D
        # acting floor (rule (b)); this test's subject is the fallback
        # removal, not the gate (test_injection_gate.py owns that).
        await agent._episodic_memory.store_note(
            topic="architecture",
            content="Consider event sourcing for architecture",
            protection_level="public",
        )

        event = AgentEvent(
            event_type=EventType.TASK_ASSIGNED,
            payload={"task": "architecture"},
        )
        with patch.object(agent, "_format_event", return_value="architecture"):
            await agent._inject_memory_context(event)

        section = agent._working_memory.get_section("recent_notes")
        assert section is not None
        assert "event sourcing" in section.content
        await agent.close_memory()

    async def test_recency_fallback_returns_empty_when_no_notes_exist(self):
        """Empty notes table → no ``recent_notes`` section.

        Trivially true post-PR 4: ``recall_notes`` returns ``[]`` when the
        table is empty regardless of ``min_score``, and the no-results path
        skips section injection.  Kept as a regression guard against the
        section being created with empty content.
        """
        agent = create_persona_agent(
            agent_id="ember-owl", config=_PERSONA_CONFIG, llm_client=_make_client(),
        )
        await agent.initialize_memory()

        event = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "hello"},
        )
        with patch.object(agent, "_format_event", return_value="hello"):
            await agent._inject_memory_context(event)

        section = agent._working_memory.get_section("recent_notes")
        assert section is None, (
            "Empty notes table must not produce a recent_notes section."
        )
        await agent.close_memory()

    async def test_low_signal_query_admits_no_notes_regardless_of_count(self):
        """Low-signal query with many notes: no notes injected (fallback removed, PR 4).

        Previously the recency fallback capped at limit=3 to limit prompt inflation.
        PR 4 removes the fallback entirely: low-signal queries produce no section
        regardless of how many notes are stored.
        (Replaces: test_recency_fallback_limits_to_three.)
        """
        agent = create_persona_agent(
            agent_id="ember-owl", config=_PERSONA_CONFIG, llm_client=_make_client(),
        )
        await agent.initialize_memory()

        for i in range(6):
            await agent._episodic_memory.store_note(
                topic=f"topic-{i}",
                content=f"Note content number {i} about something unique",
            )

        event = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "greetings"},
        )
        with patch.object(agent, "_format_event", return_value="greetings"):
            await agent._inject_memory_context(event)

        # Fallback removed: low-signal 'greetings' produces no section.
        section = agent._working_memory.get_section("recent_notes")
        assert section is None, (
            "Fallback removed in PR 4: low-signal 'greetings' should not inject notes."
        )
        await agent.close_memory()

    async def test_recency_fallback_skipped_for_non_message_events(self):
        """TICK event with no keyword-overlapping notes → no section.

        Pre-PR 4 this was guaranteed by the CHANNEL_MESSAGE gate inside
        ``should_fall_back``.  Post-PR 4 the gate is gone: ``recall_notes``
        is invoked for TICK as well, but the seeded note has no token
        overlap with the TICK query, so FTS5 returns no results above
        ``_DEFAULT_NOTES_MIN_SCORE`` and no section is injected.  The test
        therefore protects the *outcome* (no noise on autonomous ticks)
        even though the underlying mechanism shifted from gate to
        threshold filter.  (PR #148 review M-2 — docstring updated.)
        """
        agent = create_persona_agent(
            agent_id="ember-owl", config=_PERSONA_CONFIG, llm_client=_make_client(),
        )
        await agent.initialize_memory()

        await agent._episodic_memory.store_note(
            topic="unrelated-topic",
            content="Some stored knowledge with no keyword overlap",
        )

        event = AgentEvent(
            event_type=EventType.TICK,
            payload={},
        )
        with patch.object(
            agent, "_format_event",
            return_value="Autonomous tick: review your goals",
        ):
            await agent._inject_memory_context(event)

        section = agent._working_memory.get_section("recent_notes")
        assert section is None, (
            "TICK events must not surface unrelated notes (filtered by "
            "min_score post-PR 4)."
        )
        await agent.close_memory()

    async def test_recency_fallback_skipped_when_episodes_retrieved(self):
        """Unrelated note is not injected when episodic recall finds matches.

        Pre-PR 4 the ``should_fall_back`` guard short-circuited the
        fallback whenever episodes were present, preventing arbitrary
        recent notes from being piled on a prompt that already had
        signal.  Post-PR 4 the fallback no longer exists; the unrelated
        note simply scores below ``_DEFAULT_NOTES_MIN_SCORE`` and is
        filtered at the DB layer.  The assertion (notes section absent)
        remains valid; only the mechanism changed.
        (PR #148 review M-2 — docstring updated.)
        """
        agent = create_persona_agent(
            agent_id="ember-owl", config=_PERSONA_CONFIG, llm_client=_make_client(),
        )
        await agent.initialize_memory()

        # Store an episode whose summary will FTS5-match the query.
        await agent._episodic_memory.store_episode(
            summary="discussion about pottery glaze chemistry",
            context={},
            importance=0.8,
        )
        # Store a note with NO keyword overlap with the query — would only
        # surface via recency fallback.
        await agent._episodic_memory.store_note(
            topic="unrelated",
            content="Completely unrelated stored knowledge",
        )

        event = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "pottery glaze"},
            # RFC 0037 §B: mirror the dispatch path's classification stamp
            # so the turn acts ``internal`` and the seeded episode passes
            # the §D gate instead of flooring to ``public`` (rule (b)).
            metadata={"channel_classification": "internal"},
        )
        with patch.object(
            agent, "_format_event",
            return_value="pottery glaze",
        ):
            await agent._inject_memory_context(event)

        # Episodic recall should have populated its section.
        ep_section = agent._working_memory.get_section("episodic_recall")
        assert ep_section is not None
        assert "pottery glaze" in ep_section.content

        # Recency fallback no longer exists; assert the unrelated note was
        # filtered by min_score rather than by the (deleted) fallback gate.
        notes_section = agent._working_memory.get_section("recent_notes")
        assert notes_section is None, (
            "Unrelated note must not be injected when episodes already match "
            "(filtered by min_score post-PR 4)."
        )
        await agent.close_memory()


# ─── Memory Namespace Property ───────────────────────────────


class TestMemoryNamespace:
    """Verify _LLMPersonaAgent.memory exposes a MemoryNamespace
    so server_servicers.py can access agent.memory.relationship
    for recording chat interactions.
    """

    async def test_memory_property_exposes_all_tiers(self):
        agent = create_persona_agent(
            agent_id="ember-owl", config=_PERSONA_CONFIG, llm_client=_make_client(),
        )
        await agent.initialize_memory()

        ns = agent.memory
        assert ns.episodic is agent._episodic_memory
        assert ns.relationship is agent._relationship_memory
        assert ns.working is agent._working_memory
        await agent.close_memory()

    async def test_hasattr_memory_returns_true(self):
        """server_servicers.py uses hasattr(agent, 'memory') guard."""
        agent = create_persona_agent(
            agent_id="ember-owl", config=_PERSONA_CONFIG, llm_client=_make_client(),
        )
        assert hasattr(agent, "memory")
        assert hasattr(agent.memory, "relationship")
        await agent.close_memory()


# ─── User-Identity Memory Instruction Tests ──────────────────


class TestUserIdentitySystemPromptInstruction:
    """Verify _build_system_prompt contains the user-identity instruction added
    to help the agent remember who it is talking to.

    The instruction tells the agent to:
    - check stored notes on first contact via recall_notes
    - store the user's real name/role immediately via store_note with topic
      'contact:<user_id>' when the user identifies themselves
    """

    async def _make_agent(self):
        agent = create_persona_agent(
            agent_id="ember-owl",
            config=_PERSONA_CONFIG,
            llm_client=_make_client(),
        )
        await agent.initialize_memory()
        return agent

    async def test_user_identity_recall_instruction_present(self):
        """System prompt instructs agent to call recall_notes at conversation start."""
        agent = await self._make_agent()
        prompt = agent._build_system_prompt()
        assert "recall_notes" in prompt
        # The instruction should mention querying by user_id to look up existing
        # contact notes before asking who the user is.
        assert "user_id" in prompt
        await agent.close_memory()

    async def test_user_identity_store_note_instruction_present(self):
        """System prompt instructs agent to call store_note with contact:<user_id> topic."""
        agent = await self._make_agent()
        prompt = agent._build_system_prompt()
        assert "contact:<user_id>" in prompt
        await agent.close_memory()

    async def test_user_identity_instruction_is_part_of_memory_section(self):
        """User-identity instruction lives in the same memory-tools paragraph."""
        agent = await self._make_agent()
        prompt = agent._build_system_prompt()
        # Both the memory-tool intro and the user-identity guidance should be in
        # the same contiguous block (no blank line between them).
        mem_tool_pos = prompt.index("MUST call store_note")
        contact_pos = prompt.index("contact:<user_id>")
        # They should be within 600 chars of each other (same paragraph).
        assert abs(mem_tool_pos - contact_pos) < 600, (
            "User-identity instruction appears to be separated from the "
            "memory-tools instruction"
        )
        await agent.close_memory()

    async def test_user_identity_instruction_absent_when_no_memory_tools(self):
        """When an agent has no memory tools the whole block is omitted."""
        agent = await self._make_agent()
        agent._memory_tools = []
        prompt = agent._build_system_prompt()
        assert "contact:<user_id>" not in prompt
        await agent.close_memory()

    async def test_user_id_attribute_described_in_prompt(self):
        """The prompt tells the agent where to find the sender's user_id."""
        agent = await self._make_agent()
        prompt = agent._build_system_prompt()
        # The instruction references the user_id attribute from the message delimiter
        assert "user_id" in prompt
        await agent.close_memory()
