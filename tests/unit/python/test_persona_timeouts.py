"""Tests for per-event and per-tick wall-clock timeouts, minimal config prompts,
and energy clamping at maximum."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.llm_client import LLMClient, LLMResponse
from agents.persona import create_persona_agent
from agents.persona_runtime.memory_context import MemoryInjectionResult
from agents.persona_types import ActionType, AgentEvent, EventType, PersonaState

from ._persona_test_helpers import _PERSONA_CONFIG, _make_client, _task

# ─── PR #54 review: per-event timeout ───────────────────────


class TestEventTimeout:
    """Verify on_event() enforces a wall-clock timeout.

    PR #54 review Must-Fix #2: unbounded LLM processing could hold the
    per-agent lock indefinitely.
    """

    async def test_timeout_returns_descriptive_action(self):
        """A slow LLM call that exceeds event_timeout produces a timeout action."""
        async def slow_llm(*args, **kwargs):
            await asyncio.sleep(10)  # will be cancelled by timeout
            return LLMResponse(text="too slow")

        mock_provider = AsyncMock()
        mock_provider.create_message = AsyncMock(side_effect=slow_llm)
        mock_provider.format_tool_definitions = MagicMock(return_value=[])
        client = LLMClient(mock_provider)

        config = {**_PERSONA_CONFIG, "event_timeout": 0.1}
        agent = create_persona_agent(
            agent_id="ember-owl", config=config, llm_client=client,
        )
        await agent.initialize_memory()

        event = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "hello"},
        )
        actions = await agent.on_event(event)
        assert len(actions) == 1
        assert actions[0].action_type == ActionType.COMPLETE_TASK
        assert "timed out" in actions[0].payload["result"].lower()
        await agent.close_memory()

    async def test_normal_event_within_timeout(self):
        """Events that complete within the timeout work normally."""
        config = {**_PERSONA_CONFIG, "event_timeout": 10.0}
        agent = create_persona_agent(
            agent_id="ember-owl", config=config, llm_client=_make_client(),
        )
        await agent.initialize_memory()

        event = AgentEvent(
            event_type=EventType.TASK_ASSIGNED,
            payload={"task": _task()},
        )
        actions = await agent.on_event(event)
        assert len(actions) >= 1
        assert actions[0].action_type == ActionType.COMPLETE_TASK
        assert "timed out" not in actions[0].payload.get("result", "").lower()
        await agent.close_memory()


# ─── PR #55 review: on_tick() timeout ───────────────────────


class TestTickTimeout:
    """Verify on_tick() enforces the same wall-clock timeout as on_event().

    Review finding F-5a-1 (resolved in PR 5b): on_tick() lacked the
    asyncio.wait_for() guard that on_event() already had, allowing a slow
    LLM to hold the per-agent lock indefinitely.
    """

    async def test_tick_timeout_returns_do_nothing(self):
        """A slow LLM during on_tick() produces DO_NOTHING after timeout."""
        async def slow_llm(*args, **kwargs):
            await asyncio.sleep(10)  # will be cancelled by timeout
            return LLMResponse(text="too slow")

        mock_provider = AsyncMock()
        mock_provider.create_message = AsyncMock(side_effect=slow_llm)
        mock_provider.format_tool_definitions = MagicMock(return_value=[])
        client = LLMClient(mock_provider)

        config = {**_PERSONA_CONFIG, "event_timeout": 0.1}
        agent = create_persona_agent(
            agent_id="ember-owl", config=config, llm_client=client,
        )
        await agent.initialize_memory()

        actions = await agent.on_tick()
        assert len(actions) == 1
        assert actions[0].action_type == ActionType.DO_NOTHING
        await agent.close_memory()

    async def test_tick_completes_within_timeout(self):
        """Ticks that complete within the timeout work normally."""
        config = {**_PERSONA_CONFIG, "event_timeout": 10.0}
        agent = create_persona_agent(
            agent_id="ember-owl", config=config, llm_client=_make_client(),
        )
        await agent.initialize_memory()

        # Patch _inject_memory_context to return non-zero tokens so the
        # RFC 0017 §F empty-context TICK short-circuit does not fire.
        with patch.object(
            agent,
            "_inject_memory_context",
            return_value=MemoryInjectionResult(memory_admitted_tokens=200),
        ):
            actions = await agent.on_tick()
        assert len(actions) >= 1
        assert actions[0].action_type == ActionType.COMPLETE_TASK
        await agent.close_memory()

    async def test_tick_timeout_no_energy_leak(self):
        """Timed-out ticks do NOT recover energy.

        Previously ``recover_energy()`` was called before ``_on_event_inner()``,
        so timed-out ticks gained +0.1 energy without executing any actions.
        Now energy is recovered only after successful completion.
        (PR #55 review: energy leak on tick timeout.)
        """
        async def slow_llm(*args, **kwargs):
            await asyncio.sleep(10)
            return LLMResponse(text="too slow")

        mock_provider = AsyncMock()
        mock_provider.create_message = AsyncMock(side_effect=slow_llm)
        mock_provider.format_tool_definitions = MagicMock(return_value=[])
        client = LLMClient(mock_provider)

        config = {**_PERSONA_CONFIG, "event_timeout": 0.1}
        agent = create_persona_agent(
            agent_id="ember-owl", config=config, llm_client=client,
        )
        await agent.initialize_memory()

        # Set energy to a known value
        agent._state.energy = 0.5
        # Patch _inject_memory_context to ensure the timeout path is tested
        # (not the short-circuit path). This ensures we test the actual
        # timeout behavior rather than the RFC 0017 §F short-circuit.
        with patch.object(
            agent,
            "_inject_memory_context",
            return_value=MemoryInjectionResult(memory_admitted_tokens=200),
        ):
            await agent.on_tick()  # times out
        # Energy must NOT have increased — timed-out tick produces no work
        assert agent._state.energy == pytest.approx(0.5)
        await agent.close_memory()

    async def test_tick_success_recovers_energy(self):
        """Successful ticks DO recover energy (after completion).

        Complements ``test_tick_timeout_no_energy_leak`` — verifies the
        normal path still recovers.
        """
        config = {**_PERSONA_CONFIG, "event_timeout": 10.0}
        agent = create_persona_agent(
            agent_id="ember-owl", config=config, llm_client=_make_client(),
        )
        await agent.initialize_memory()

        agent._state.energy = 0.5
        # Patch _inject_memory_context to return non-zero tokens so the
        # RFC 0017 §F empty-context TICK short-circuit does not fire.
        with patch.object(
            agent,
            "_inject_memory_context",
            return_value=MemoryInjectionResult(memory_admitted_tokens=200),
        ):
            await agent.on_tick()
        # Energy should have recovered (+0.1) minus drain for actions.
        # The mock LLM returns a COMPLETE_TASK action which drains 0.05,
        # so net change is +0.1 - 0.05 = +0.05.
        assert agent._state.energy == pytest.approx(0.55)
        await agent.close_memory()


# ─── F-5a-4: Minimal config prompt ────────────────────────


class TestMinimalConfigPrompt:
    """F-5a-4: System prompt with minimal persona config."""

    async def test_minimal_persona_produces_valid_prompt(self):
        """Agent with only required persona fields builds a prompt without error."""
        minimal_config = {
            "id": "minimal",
            "type": "persona",
            "name": "Minimal Agent",
            "role": "Tester",
            "model": "test-model",
            "persona": {
                "title": "Tester",
                "background": "QA.",
                "behavior": {},
            },
            "memory": {"db_path": ":memory:"},
        }
        agent = create_persona_agent(
            agent_id="minimal", config=minimal_config, llm_client=_make_client(),
        )
        prompt = agent._build_system_prompt()
        assert "Minimal Agent" in prompt
        assert "Tester" in prompt
        # No quirks, goals, or behavior should not crash
        assert isinstance(prompt, str)
        assert len(prompt) > 0


# ─── F-5a-5: Energy at exactly 1.0 ────────────────────────


class TestEnergyClampAtOne:
    """F-5a-5: recover_energy() when energy is already at 1.0."""

    def test_recover_at_max_stays_at_one(self):
        state = PersonaState(energy=1.0)
        state.recover_energy()
        assert state.energy == pytest.approx(1.0)

    def test_recover_near_max_clamps(self):
        state = PersonaState(energy=0.99)
        state.recover_energy()
        assert state.energy == pytest.approx(1.0)
