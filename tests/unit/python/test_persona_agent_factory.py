"""Tests for create_persona_agent factory, energy mechanics, event formatting,
LLM call limits, fallback limits, convenience methods, and utility helpers."""

import pytest

from agents.llm_client import LLMResponse, StopReason, ToolCall, Usage
from agents.persona import create_persona_agent
from agents.persona_runtime import _coerce_event_timeout, _LLMPersonaAgent, _truncate_with_ellipsis
from agents.persona_types import ActionType, AgentEvent, EventType, PersonaState

from ._persona_test_helpers import _PERSONA_CONFIG, _make_client

# ─── Factory Tests ──────────────────────────────────────────


class TestCreatePersonaAgent:
    async def test_returns_llm_persona_agent(self):
        agent = create_persona_agent(
            agent_id="ember-owl",
            config=_PERSONA_CONFIG,
            llm_client=_make_client(),
        )
        assert isinstance(agent, _LLMPersonaAgent)

    async def test_memory_tiers_wired(self):
        agent = create_persona_agent(
            agent_id="ember-owl",
            config=_PERSONA_CONFIG,
            llm_client=_make_client(),
        )
        assert agent._episodic_memory is not None
        assert agent._relationship_memory is not None
        assert agent._working_memory is not None

    async def test_memory_tools_created(self):
        agent = create_persona_agent(
            agent_id="ember-owl",
            config=_PERSONA_CONFIG,
            llm_client=_make_client(),
        )
        tool_names = {td.name for td in agent._memory_tools}
        assert "store_note" in tool_names
        assert "recall_notes" in tool_names

    async def test_initialize_and_close_memory(self):
        agent = create_persona_agent(
            agent_id="ember-owl",
            config=_PERSONA_CONFIG,
            llm_client=_make_client(),
        )
        await agent.initialize_memory()
        await agent.close_memory()

    async def test_default_memory_config(self):
        """Agent without explicit memory config uses defaults."""
        config = {
            "id": "minimal-agent",
            "type": "persona",
            "name": "Minimal",
            "role": "Test",
            "model": "test-model",
            "persona": {"behavior": {}},
        }
        agent = create_persona_agent(
            agent_id="minimal-agent",
            config=config,
            llm_client=_make_client(),
        )
        assert agent._working_memory.max_tokens == 100_000

    async def test_working_memory_not_conflated_with_llm_max_tokens(self):
        """F-5a-1: config['max_tokens'] is the LLM completion limit, not the
        working memory budget. Working memory should read from
        memory.working.max_tokens instead."""
        config = {
            **_PERSONA_CONFIG,
            "max_tokens": 4096,  # LLM completion limit — must NOT affect working memory
        }
        agent = create_persona_agent(
            agent_id="ember-owl",
            config=config,
            llm_client=_make_client(),
        )
        # Should be the default 100_000, NOT 4096
        assert agent._working_memory.max_tokens == 100_000

    async def test_working_memory_reads_from_memory_config(self):
        """F-5a-1: Working memory budget is configured under memory.working.max_tokens."""
        config = {
            **_PERSONA_CONFIG,
            "max_tokens": 4096,
            "memory": {
                **_PERSONA_CONFIG["memory"],
                "working": {"max_tokens": 50_000},
            },
        }
        agent = create_persona_agent(
            agent_id="ember-owl",
            config=config,
            llm_client=_make_client(),
        )
        assert agent._working_memory.max_tokens == 50_000


# ─── Lazy Energy Recovery Tests ────────────────────────────


class TestEnergyMechanics:
    def test_drain_20_times_reaches_zero(self):
        state = PersonaState(energy=1.0)
        for _ in range(20):
            state.drain_energy()
        assert state.energy == 0.0

    def test_recover_10_times_reaches_one(self):
        state = PersonaState(energy=0.0)
        for _ in range(10):
            state.recover_energy()
        assert state.energy == pytest.approx(1.0)

    def test_alternating_drain_recover(self):
        state = PersonaState(energy=0.5)
        state.drain_energy()   # 0.45
        state.recover_energy()  # 0.55
        assert state.energy == pytest.approx(0.55)


# ─── Review follow-up: additional _format_event coverage ────


class TestFormatEventAdditional:
    """Tests for _format_event() event types not covered above (review finding #7)."""

    async def _make_agent(self) -> _LLMPersonaAgent:
        agent = create_persona_agent(
            agent_id="ember-owl",
            config=_PERSONA_CONFIG,
            llm_client=_make_client(),
        )
        await agent.initialize_memory()
        return agent

    async def test_format_event_sub_agent_completed(self):
        agent = await self._make_agent()
        event = AgentEvent(
            event_type=EventType.SUB_AGENT_COMPLETED,
            payload={"result": "Code review complete, no issues found."},
        )
        msg = agent._format_event(event)
        assert "sub-agent completed" in msg
        assert "Code review complete" in msg
        await agent.close_memory()

    async def test_format_event_catch_all(self):
        """Catch-all path formats unknown/future event types via JSON."""
        agent = await self._make_agent()
        event = AgentEvent(
            event_type=EventType.AGENT_JOINED,
            payload={"agent_id": "new-agent", "role": "reviewer"},
        )
        msg = agent._format_event(event)
        assert "agent_joined" in msg
        assert "new-agent" in msg
        await agent.close_memory()

    async def test_format_event_catch_all_non_serializable_payload(self):
        """Non-JSON-serializable payload falls back to str() (review finding #5)."""
        agent = await self._make_agent()
        # object() is not JSON-serializable
        event = AgentEvent(
            event_type=EventType.AGENT_LEFT,
            payload={"agent": object()},
        )
        # Should not raise — falls back to str()
        msg = agent._format_event(event)
        assert "agent_left" in msg
        await agent.close_memory()


class TestMaxLLMCallsExhaustion:
    """Test that the max_llm_calls guard prevents infinite tool loops (review finding #6)."""

    async def test_max_llm_calls_exhaustion(self):
        """LLM always returns TOOL_USE — loop bounded by max_llm_calls."""
        # Create responses that always request tool use
        tool_response = LLMResponse(
            text=None,
            tool_calls=[ToolCall(id="tc1", name="recall_notes", input={"query": "x"})],
            stop_reason=StopReason.TOOL_USE,
            usage=Usage(100, 50),
        )
        max_calls = 3
        # Provide enough responses; the loop should break after max_calls
        responses = [tool_response] * (max_calls + 1)
        client = _make_client(responses)

        config = {**_PERSONA_CONFIG, "max_llm_calls": max_calls}
        agent = create_persona_agent(
            agent_id="ember-owl", config=config, llm_client=client,
        )
        await agent.initialize_memory()

        event = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "test"},
            sender_id="test",
        )
        actions = await agent.on_event(event)

        # LLM was called exactly max_calls times (loop bounded)
        assert client._provider.create_message.call_count == max_calls
        # Should still return valid actions (parse_actions handles tool_use response)
        assert len(actions) >= 1
        # Review finding fix: exhaustion now produces a descriptive result
        # instead of an empty COMPLETE_TASK
        assert actions[0].action_type == ActionType.COMPLETE_TASK
        assert "Max LLM call budget exhausted" in actions[0].payload["result"]
        await agent.close_memory()


# ─── PR #95 review: persona fallback limits regression test ──


class TestPersonaDefaultFallbackLimits:
    """Persona agents that omit max_llm_calls / max_tokens must fall back to
    the original persona-runtime defaults (10 / 4096), NOT the task-agent
    defaults in defaults.py (5 / 8192).

    (PR #95 review finding: shared defaults silently changed persona behavior.)
    """

    async def test_fallback_max_llm_calls_is_10(self):
        """Without explicit max_llm_calls the persona loop runs up to 10 times."""
        client = _make_client()
        config = {k: v for k, v in _PERSONA_CONFIG.items() if k != "max_llm_calls"}
        agent = create_persona_agent(
            agent_id="ember-owl", config=config, llm_client=client,
        )
        await agent.initialize_memory()

        event = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "hello"},
            sender_id="test",
        )
        await agent.on_event(event)
        # Single END_TURN response means 1 call, but the loop limit must be 10.
        # Verify via the action_loop constant directly.
        from agents.persona_runtime.action_loop import _PERSONA_DEFAULT_MAX_LLM_CALLS
        assert _PERSONA_DEFAULT_MAX_LLM_CALLS == 10
        await agent.close_memory()

    async def test_fallback_max_tokens_is_4096(self):
        """Without explicit max_tokens the persona loop uses 4096."""
        client = _make_client()
        config = {k: v for k, v in _PERSONA_CONFIG.items() if k != "max_tokens"}
        agent = create_persona_agent(
            agent_id="ember-owl", config=config, llm_client=client,
        )
        await agent.initialize_memory()

        event = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "hello"},
            sender_id="test",
        )
        await agent.on_event(event)
        # Verify the LLM was called with max_tokens=4096 (the persona default)
        call_kwargs = client._provider.create_message.call_args
        assert (
            call_kwargs.kwargs.get("max_tokens") == 4096
            or call_kwargs[1].get("max_tokens") == 4096
        )
        await agent.close_memory()


# ─── Review follow-up: convenience method tests ─────────────


class TestConvenienceMethods:
    """Tests for PersonaAgent.message(), complete(), delegate_to() (review finding).

    These action constructors are part of the public API. Verifying their
    structure ensures downstream action executors receive correct payloads.
    """

    async def _make_agent(self) -> _LLMPersonaAgent:
        agent = create_persona_agent(
            agent_id="ember-owl",
            config=_PERSONA_CONFIG,
            llm_client=_make_client(),
        )
        await agent.initialize_memory()
        return agent

    async def test_message_action(self):
        agent = await self._make_agent()
        action = agent.message("ch-1", "Hello team", mentions=["mike"])
        assert action.action_type == ActionType.SEND_CHANNEL_MESSAGE
        assert action.payload["channel_id"] == "ch-1"
        assert action.payload["content"] == "Hello team"
        assert action.payload["type"] == "TEXT"
        assert action.payload["mentions"] == ["mike"]
        await agent.close_memory()

    async def test_complete_action(self):
        agent = await self._make_agent()
        action = agent.complete("task done", confidence=0.9)
        assert action.action_type == ActionType.COMPLETE_TASK
        assert action.payload["result"] == "task done"
        assert action.payload["metadata"] == {"confidence": 0.9}
        await agent.close_memory()

    async def test_delegate_to_action(self):
        agent = await self._make_agent()
        action = agent.delegate_to("coder-agent", "Implement the feature")
        assert action.action_type == ActionType.DELEGATE
        assert action.payload["agent_id"] == "coder-agent"
        assert action.payload["task"] == "Implement the feature"
        await agent.close_memory()


# ─── _truncate_with_ellipsis() unit tests ────────────────────


class TestTruncateWithEllipsis:
    """Dedicated unit tests for the _truncate_with_ellipsis() helper.

    The helper is used in 3 critical paths inside _inject_memory_context()
    (episode summaries, relationship notes, note content).  Direct tests
    prevent regressions that would silently corrupt memory context.
    (PR #60 review: _truncate_with_ellipsis has no dedicated unit tests.)
    """

    def test_short_text_unchanged(self):
        """Text within the limit is returned as-is."""
        assert _truncate_with_ellipsis("hello", 10) == "hello"

    def test_exact_length_unchanged(self):
        """Text exactly at the limit is NOT truncated."""
        text = "abcde"
        assert _truncate_with_ellipsis(text, 5) == "abcde"

    def test_long_text_truncated_at_word_boundary(self):
        """Text exceeding the limit is cut at the last word boundary."""
        text = "the quick brown fox jumps over the lazy dog"
        result = _truncate_with_ellipsis(text, 15)
        # "the quick brown" is 15 chars; rsplit at last space → "the quick"
        assert result == "the quick..."

    def test_no_space_in_slice_uses_full_slice(self):
        """Text without spaces falls back to hard slice at max_chars."""
        text = "abcdefghijklmnopqrstuvwxyz"
        result = _truncate_with_ellipsis(text, 10)
        assert result == "abcdefghij..."

    def test_empty_string(self):
        """Empty string is returned unchanged."""
        assert _truncate_with_ellipsis("", 10) == ""

    def test_single_char_limit(self):
        """max_chars=1 with multi-char text truncates correctly."""
        result = _truncate_with_ellipsis("hello world", 1)
        # Slice is "h", no space → full slice used.
        assert result == "h..."

    def test_ellipsis_always_appended_on_truncation(self):
        """Truncated text always ends with '...'."""
        result = _truncate_with_ellipsis("a b c d e f g h", 5)
        assert result.endswith("...")


class TestCoerceEventTimeout:
    """Verify _coerce_event_timeout() handles various input types.

    Extracted from on_event()/on_tick() where the same try/float() guard
    was duplicated.  Tests ensure the helper handles YAML-sourced strings,
    valid numerics, and invalid types without duplicating coverage already
    in TestEventTimeout/TestTickTimeout (which test the full on_event/on_tick
    paths).
    (PR #60 review: timeout coercion duplicated between on_event/on_tick.)
    """

    def test_float_passthrough(self):
        assert _coerce_event_timeout(300.0, 100.0, "test") == 300.0

    def test_int_coerced(self):
        assert _coerce_event_timeout(60, 100.0, "test") == 60.0

    def test_string_coerced(self):
        """YAML configs can supply '300' as a string for a numeric key."""
        assert _coerce_event_timeout("300", 100.0, "test") == 300.0

    def test_invalid_string_returns_default(self):
        assert _coerce_event_timeout("not-a-number", 100.0, "test") == 100.0

    def test_none_returns_default(self):
        assert _coerce_event_timeout(None, 100.0, "test") == 100.0
