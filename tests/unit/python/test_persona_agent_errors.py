"""Tests for persona agent error handling: memory close failures, corrupted state,
MAX_TOKENS stop reason, missing model config, and action payload validation."""

import json
from unittest.mock import AsyncMock

import pytest

from agents.llm_client import LLMResponse, StopReason, ToolCall
from agents.persona import create_persona_agent
from agents.persona_types import ActionType, AgentEvent, EventType, Mood

from ._persona_test_helpers import _PERSONA_CONFIG, _make_client, _task


# ─── PR #54 review: close_memory() partial tier failure ──────


class TestCloseMemoryPartialFailure:
    """Verify close_memory() closes all tiers even if one raises.

    PR #54 review finding #3: sequential close without try/finally meant a
    failure in an earlier tier would leak later tiers' DB connections.
    """

    async def test_later_tiers_closed_when_earlier_tier_raises(self):
        """If episodic close() raises, relationship memory is still closed."""
        agent = create_persona_agent(
            agent_id="ember-owl",
            config=_PERSONA_CONFIG,
            llm_client=_make_client(),
        )
        await agent.initialize_memory()

        # Sabotage episodic close to raise
        agent._episodic_memory.close = AsyncMock(side_effect=RuntimeError("disk full"))

        # Spy on relationship close
        original_rel_close = agent._relationship_memory.close
        rel_close_called = False

        async def _tracking_rel_close() -> None:
            nonlocal rel_close_called
            rel_close_called = True
            await original_rel_close()

        agent._relationship_memory.close = _tracking_rel_close  # type: ignore[assignment]

        # close_memory() should NOT raise
        await agent.close_memory()
        assert rel_close_called, "relationship memory was never closed"

    async def test_all_tiers_attempted_when_working_memory_fails(self):
        """If working memory close() raises, episodic and relationship are still closed."""
        agent = create_persona_agent(
            agent_id="ember-owl",
            config=_PERSONA_CONFIG,
            llm_client=_make_client(),
        )
        await agent.initialize_memory()

        agent._working_memory.close = AsyncMock(side_effect=RuntimeError("boom"))

        ep_close_called = False
        original_ep_close = agent._episodic_memory.close

        async def _tracking_ep_close() -> None:
            nonlocal ep_close_called
            ep_close_called = True
            await original_ep_close()

        agent._episodic_memory.close = _tracking_ep_close  # type: ignore[assignment]

        await agent.close_memory()
        assert ep_close_called, "episodic memory was never closed"


# ─── PR #54 review: corrupted JSON in _load_persona_state() ──


class TestLoadPersonaStateCorrupted:
    """Verify _load_persona_state() returns defaults for corrupted DB data.

    PR #54 review finding #12: the except-Exception branch in
    _load_persona_state() was untested — a corrupted JSON string in
    agent_state should fall back to defaults without propagating.
    """

    async def test_corrupted_json_returns_defaults(self):
        agent = create_persona_agent(
            agent_id="ember-owl",
            config=_PERSONA_CONFIG,
            llm_client=_make_client(),
        )
        await agent.initialize_memory()

        # Write corrupted JSON directly via the episodic memory API
        await agent._episodic_memory.persist_agent_state(
            agent.agent_id, "not-valid-json!!{",
        )

        state = await agent._load_persona_state()
        assert state.mood == Mood.NEUTRAL
        assert state.energy == 1.0
        assert state.stress_level == 0.0
        await agent.close_memory()


# ─── PR #54 review: MAX_TOKENS stop_reason handling ─────────


class TestMaxTokensStopReason:
    """Verify _on_event_inner() returns a descriptive action when the LLM
    truncates its response (stop_reason=MAX_TOKENS).

    PR #54 review Must-Fix #1: truncated text should not be parsed as
    actions — it could produce malformed JSON that silently falls back to
    COMPLETE_TASK with garbage content.  Consistent with
    BaseAgent._run_llm_loop() which returns FAILED for MAX_TOKENS.
    """

    async def test_max_tokens_returns_truncated_action(self):
        truncated_response = LLMResponse(
            text='[{"action_type": "send_messa',  # truncated mid-JSON
            stop_reason=StopReason.MAX_TOKENS,
        )
        client = _make_client(responses=[truncated_response])
        agent = create_persona_agent(
            agent_id="ember-owl",
            config=_PERSONA_CONFIG,
            llm_client=client,
        )
        await agent.initialize_memory()
        event = AgentEvent(event_type=EventType.TASK_ASSIGNED, payload={"task": _task()})
        actions = await agent.on_event(event)

        assert len(actions) == 1
        assert actions[0].action_type == ActionType.COMPLETE_TASK
        assert "truncated" in actions[0].payload["result"].lower()
        assert "max_tokens" in actions[0].payload["result"]
        await agent.close_memory()

    async def test_max_tokens_after_tool_use_round(self):
        """MAX_TOKENS on the second LLM call (after a tool round) should
        still be caught and produce the descriptive action."""
        tool_response = LLMResponse(
            text="",
            stop_reason=StopReason.TOOL_USE,
            tool_calls=[ToolCall(id="tc1", name="unknown_tool", input={})],
        )
        truncated_response = LLMResponse(
            text="partial output...",
            stop_reason=StopReason.MAX_TOKENS,
        )
        client = _make_client(responses=[tool_response, truncated_response])
        agent = create_persona_agent(
            agent_id="ember-owl",
            config=_PERSONA_CONFIG,
            llm_client=client,
        )
        await agent.initialize_memory()
        event = AgentEvent(event_type=EventType.TASK_ASSIGNED, payload={"task": _task()})
        actions = await agent.on_event(event)

        assert len(actions) == 1
        assert actions[0].action_type == ActionType.COMPLETE_TASK
        assert "truncated" in actions[0].payload["result"].lower()
        await agent.close_memory()


# ─── PR #54 review: missing 'model' config key ──────────────


class TestMissingModelConfig:
    """Verify _on_event_inner() fails fast with a descriptive message when
    the agent config is missing the required 'model' field.

    PR #54 review Must-Fix #2: a bare KeyError from self.config['model']
    produces an unclear traceback.  The fail-fast check matches the
    BaseAgent._run_llm_loop() SF2 pattern.
    """

    async def test_missing_model_returns_descriptive_error(self):
        config_without_model = {**_PERSONA_CONFIG}
        del config_without_model["model"]

        agent = create_persona_agent(
            agent_id="ember-owl",
            config=config_without_model,
            llm_client=_make_client(),
        )
        await agent.initialize_memory()
        event = AgentEvent(event_type=EventType.TASK_ASSIGNED, payload={"task": _task()})
        actions = await agent.on_event(event)

        assert len(actions) == 1
        assert actions[0].action_type == ActionType.COMPLETE_TASK
        assert "model" in actions[0].payload["result"].lower()
        await agent.close_memory()


# ─── PR #54 review: action payload validation ───────────────


class TestActionPayloadValidation:
    """Verify _validate_action_payload() rejects malformed LLM-generated payloads.

    PR #54 review Must-Fix #1: DELEGATE, SEND_MESSAGE, SPAWN_SUB_AGENT payloads
    must contain required fields. Invalid payloads are replaced with DO_NOTHING.
    """

    async def _make_agent(self):
        cfg = {**_PERSONA_CONFIG}
        agent = create_persona_agent(
            agent_id=cfg["id"], config=cfg, llm_client=_make_client(),
        )
        await agent.initialize_memory()
        return agent

    async def test_delegate_valid(self):
        agent = await self._make_agent()
        response = LLMResponse(text=json.dumps([
            {"action_type": "delegate", "payload": {"agent_id": "my-agent", "task": "do stuff"}},
        ]))
        actions = agent._parse_actions(response)
        assert actions[0].action_type == ActionType.DELEGATE

    async def test_delegate_missing_agent_id(self):
        agent = await self._make_agent()
        response = LLMResponse(text=json.dumps([
            {"action_type": "delegate", "payload": {"task": "do stuff"}},
        ]))
        actions = agent._parse_actions(response)
        assert actions[0].action_type == ActionType.DO_NOTHING

    async def test_delegate_invalid_agent_id_format(self):
        agent = await self._make_agent()
        response = LLMResponse(text=json.dumps([
            {"action_type": "delegate", "payload": {"agent_id": "UPPER_CASE!", "task": "do stuff"}},
        ]))
        actions = agent._parse_actions(response)
        assert actions[0].action_type == ActionType.DO_NOTHING

    async def test_delegate_single_char_agent_id_accepted(self):
        """F-6a-2: single character agent IDs are now valid."""
        agent = await self._make_agent()
        response = LLMResponse(text=json.dumps([
            {"action_type": "delegate", "payload": {"agent_id": "a", "task": "do stuff"}},
        ]))
        actions = agent._parse_actions(response)
        assert actions[0].action_type == ActionType.DELEGATE

    async def test_delegate_missing_task(self):
        agent = await self._make_agent()
        response = LLMResponse(text=json.dumps([
            {"action_type": "delegate", "payload": {"agent_id": "my-agent"}},
        ]))
        actions = agent._parse_actions(response)
        assert actions[0].action_type == ActionType.DO_NOTHING

    async def test_delegate_empty_task(self):
        agent = await self._make_agent()
        response = LLMResponse(text=json.dumps([
            {"action_type": "delegate", "payload": {"agent_id": "my-agent", "task": "  "}},
        ]))
        actions = agent._parse_actions(response)
        assert actions[0].action_type == ActionType.DO_NOTHING

    async def test_send_message_valid(self):
        agent = await self._make_agent()
        response = LLMResponse(text=json.dumps([
            {"action_type": "send_message", "payload": {"channel_id": "general", "content": "hello"}},
        ]))
        actions = agent._parse_actions(response)
        assert actions[0].action_type == ActionType.SEND_MESSAGE

    async def test_send_message_missing_channel_id(self):
        agent = await self._make_agent()
        response = LLMResponse(text=json.dumps([
            {"action_type": "send_message", "payload": {"content": "hello"}},
        ]))
        actions = agent._parse_actions(response)
        assert actions[0].action_type == ActionType.DO_NOTHING

    async def test_send_message_missing_content(self):
        agent = await self._make_agent()
        response = LLMResponse(text=json.dumps([
            {"action_type": "send_message", "payload": {"channel_id": "general"}},
        ]))
        actions = agent._parse_actions(response)
        assert actions[0].action_type == ActionType.DO_NOTHING

    async def test_send_message_empty_content(self):
        agent = await self._make_agent()
        response = LLMResponse(text=json.dumps([
            {"action_type": "send_message", "payload": {"channel_id": "general", "content": ""}},
        ]))
        actions = agent._parse_actions(response)
        assert actions[0].action_type == ActionType.DO_NOTHING

    async def test_spawn_sub_agent_valid(self):
        agent = await self._make_agent()
        response = LLMResponse(text=json.dumps([
            {"action_type": "spawn_sub_agent", "payload": {"role": "researcher", "task": "find info"}},
        ]))
        actions = agent._parse_actions(response)
        assert actions[0].action_type == ActionType.SPAWN_SUB_AGENT

    async def test_spawn_sub_agent_missing_role(self):
        agent = await self._make_agent()
        response = LLMResponse(text=json.dumps([
            {"action_type": "spawn_sub_agent", "payload": {"task": "find info"}},
        ]))
        actions = agent._parse_actions(response)
        assert actions[0].action_type == ActionType.DO_NOTHING

    async def test_spawn_sub_agent_missing_task(self):
        agent = await self._make_agent()
        response = LLMResponse(text=json.dumps([
            {"action_type": "spawn_sub_agent", "payload": {"role": "researcher"}},
        ]))
        actions = agent._parse_actions(response)
        assert actions[0].action_type == ActionType.DO_NOTHING

    async def test_complete_task_passes_through(self):
        """COMPLETE_TASK has no payload constraints — always passes."""
        agent = await self._make_agent()
        response = LLMResponse(text=json.dumps([
            {"action_type": "complete_task", "payload": {}},
        ]))
        actions = agent._parse_actions(response)
        assert actions[0].action_type == ActionType.COMPLETE_TASK

    async def test_do_nothing_passes_through(self):
        agent = await self._make_agent()
        response = LLMResponse(text=json.dumps([
            {"action_type": "do_nothing", "payload": {}},
        ]))
        actions = agent._parse_actions(response)
        assert actions[0].action_type == ActionType.DO_NOTHING

    async def test_mixed_valid_and_invalid(self):
        """Valid actions pass, invalid are replaced with DO_NOTHING."""
        agent = await self._make_agent()
        response = LLMResponse(text=json.dumps([
            {"action_type": "delegate", "payload": {"agent_id": "INVALID!", "task": "x"}},
            {"action_type": "complete_task", "payload": {"result": "ok"}},
        ]))
        actions = agent._parse_actions(response)
        assert len(actions) == 2
        assert actions[0].action_type == ActionType.DO_NOTHING
        assert actions[1].action_type == ActionType.COMPLETE_TASK
