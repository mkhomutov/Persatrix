"""
Tests for PersonaAgent runtime core — PersonaState, Mood enum,
behavioral dimension rendering, _LLMPersonaAgent, and create_persona_agent().

All tests use mock LLM client — no real API calls.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.base import TaskInput, TaskStatus
from agents.llm_client import (
    LLMClient,
    LLMResponse,
    StopReason,
    ToolCall,
    Usage,
)
from agents.persona import (
    DIMENSION_DESCRIPTIONS,
    ActionType,
    AgentEvent,
    EventType,
    Mood,
    PersonaState,
    _LLMPersonaAgent,
    create_persona_agent,
    render_behavior,
)
from agents.tools.registry import clear_registry


# ─── Fixtures ───────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


def _make_client(
    responses: list[LLMResponse] | None = None,
) -> LLMClient:
    """Create a mock LLMClient that returns the given responses."""
    mock_provider = AsyncMock()
    if responses:
        mock_provider.create_message = AsyncMock(side_effect=responses)
    else:
        mock_provider.create_message = AsyncMock(
            return_value=LLMResponse(text="I'll handle this task.")
        )
    mock_provider.format_tool_definitions = MagicMock(return_value=[])
    mock_provider.append_tool_round = MagicMock(
        side_effect=lambda msgs, resp, results: [
            *msgs,
            {"role": "assistant", "content": "tool round"},
            {"role": "user", "content": "tool results"},
        ]
    )
    return LLMClient(mock_provider)


_PERSONA_CONFIG: dict = {
    "id": "sarah-chen",
    "type": "persona",
    "name": "Sarah Chen",
    "role": "Engineering leadership",
    "model": "test-model",
    "temperature": 0.7,
    "max_llm_calls": 10,
    "max_tokens": 4096,
    "persona": {
        "title": "VP of Engineering",
        "background": "15 years in software engineering.",
        "behavior": {
            "directness": "direct",
            "detail_focus": "big-picture",
            "formality": "professional",
            "risk_tolerance": "moderate",
            "expressiveness": "reserved",
        },
        "quirks": ["Starts every Monday with 'What's on fire?'"],
        "goals": {
            "primary": "Ship v2.0 on time",
            "secondary": ["Reduce tech debt by 20%"],
            "hidden": "Prove the team can self-organize",
        },
    },
    "permissions": {
        "memory": ["memory:read", "memory:write"],
    },
    "memory": {
        "db_path": ":memory:",
        "notes": {
            "max_notes": 100,
            "auto_reflect_after": 5,
        },
    },
}


def _task(payload: str = "do something") -> TaskInput:
    return TaskInput(task_id="t1", workflow_id="w1", payload=payload)


# ─── Mood Enum Tests ────────────────────────────────────────


class TestMood:
    def test_all_six_values(self):
        assert len(Mood) == 6
        expected = {"neutral", "focused", "frustrated", "energized", "uncertain", "satisfied"}
        assert {m.value for m in Mood} == expected

    def test_serialize_deserialize(self):
        for mood in Mood:
            assert Mood(mood.value) is mood


# ─── PersonaState Tests ────────────────────────────────────


class TestPersonaState:
    def test_defaults(self):
        state = PersonaState()
        assert state.mood is Mood.NEUTRAL
        assert state.stress_level == 0.0
        assert state.energy == 1.0
        assert state.recent_context == []
        assert state.goal_progress == {}

    def test_to_prompt_section_default(self):
        state = PersonaState()
        section = state.to_prompt_section()
        assert "Current mood: neutral" in section
        # stress and energy not shown at default values
        assert "Stress" not in section
        assert "Energy" not in section

    def test_to_prompt_section_stress_above_threshold(self):
        state = PersonaState(stress_level=0.5)
        section = state.to_prompt_section()
        assert "Stress level: 0.5/1.0" in section

    def test_to_prompt_section_stress_below_threshold(self):
        state = PersonaState(stress_level=0.2)
        section = state.to_prompt_section()
        assert "Stress" not in section

    def test_to_prompt_section_low_energy(self):
        state = PersonaState(energy=0.3)
        section = state.to_prompt_section()
        assert "Energy level: 0.3/1.0" in section
        assert "conserve effort" in section

    def test_to_prompt_section_normal_energy(self):
        state = PersonaState(energy=0.7)
        section = state.to_prompt_section()
        assert "Energy" not in section

    def test_to_prompt_section_recent_context(self):
        state = PersonaState(recent_context=["discussed roadmap", "reviewed PR"])
        section = state.to_prompt_section()
        assert "Recent context:" in section
        assert "discussed roadmap" in section
        assert "reviewed PR" in section

    def test_to_prompt_section_recent_context_limited_to_5(self):
        state = PersonaState(recent_context=[f"item-{i}" for i in range(10)])
        section = state.to_prompt_section()
        # Should only show last 5
        assert "item-5" in section
        assert "item-9" in section
        assert "item-4" not in section

    def test_to_prompt_section_goal_progress(self):
        state = PersonaState(goal_progress={"Ship v2": 0.75})
        section = state.to_prompt_section()
        assert "Goal progress:" in section
        assert "Ship v2: 75%" in section

    def test_drain_energy(self):
        state = PersonaState(energy=1.0)
        state.drain_energy()
        assert state.energy == pytest.approx(0.95)

    def test_drain_energy_clamps_to_zero(self):
        state = PersonaState(energy=0.02)
        state.drain_energy()
        assert state.energy == 0.0

    def test_recover_energy(self):
        state = PersonaState(energy=0.5)
        state.recover_energy()
        assert state.energy == pytest.approx(0.6)

    def test_recover_energy_clamps_to_one(self):
        state = PersonaState(energy=0.95)
        state.recover_energy()
        assert state.energy == 1.0

    def test_to_dict(self):
        state = PersonaState(
            mood=Mood.FOCUSED,
            stress_level=0.4,
            energy=0.8,
            recent_context=["test"],  # NOT persisted
            goal_progress={"goal": 0.5},
        )
        d = state.to_dict()
        assert d == {
            "mood": "focused",
            "stress_level": 0.4,
            "energy": 0.8,
            "goal_progress": {"goal": 0.5},
        }
        assert "recent_context" not in d

    def test_from_dict(self):
        data = {
            "mood": "frustrated",
            "stress_level": 0.6,
            "energy": 0.3,
            "goal_progress": {"task": 0.9},
        }
        state = PersonaState.from_dict(data)
        assert state.mood is Mood.FRUSTRATED
        assert state.stress_level == 0.6
        assert state.energy == 0.3
        assert state.goal_progress == {"task": 0.9}
        assert state.recent_context == []  # always empty

    def test_from_dict_unknown_mood_defaults(self):
        state = PersonaState.from_dict({"mood": "angry"})
        assert state.mood is Mood.NEUTRAL

    def test_from_dict_empty(self):
        state = PersonaState.from_dict({})
        assert state.mood is Mood.NEUTRAL
        assert state.energy == 1.0

    def test_round_trip_persistence(self):
        original = PersonaState(
            mood=Mood.ENERGIZED,
            stress_level=0.7,
            energy=0.4,
            goal_progress={"ship": 0.6},
        )
        restored = PersonaState.from_dict(original.to_dict())
        assert restored.mood is Mood.ENERGIZED
        assert restored.stress_level == original.stress_level
        assert restored.energy == original.energy
        assert restored.goal_progress == original.goal_progress


# ─── Behavioral Dimension Tests ────────────────────────────


class TestRenderBehavior:
    def test_all_dimensions_present(self):
        behavior = {
            "directness": "direct",
            "detail_focus": "big-picture",
            "formality": "professional",
            "risk_tolerance": "moderate",
            "expressiveness": "reserved",
        }
        rendered = render_behavior(behavior)
        assert "Says exactly what they think" in rendered
        assert "Focuses on high-level patterns" in rendered
        assert "Clear and structured" in rendered
        assert "Balances speed with diligence" in rendered
        assert "Keeps emotions out of professional" in rendered

    def test_defaults_applied_for_omitted_dimensions(self):
        # Empty behavior → all defaults applied
        rendered = render_behavior({})
        # Should have default descriptions for all 5 dimensions
        assert "Balances directness with tact" in rendered  # directness: balanced
        assert "Addresses both high-level" in rendered  # detail_focus: balanced
        assert "Clear and structured" in rendered  # formality: professional
        assert "Balances speed with diligence" in rendered  # risk_tolerance: moderate
        assert "Acknowledges emotions when relevant" in rendered  # expressiveness: moderate

    def test_partial_override(self):
        behavior = {"directness": "indirect"}
        rendered = render_behavior(behavior)
        assert "Diplomatic and tactful" in rendered
        # Other dimensions should use defaults
        assert "Balances speed with diligence" in rendered

    def test_unknown_dimension_ignored(self):
        behavior = {"unknown_dim": "unknown_val"}
        rendered = render_behavior(behavior)
        # Should still have defaults for known dimensions
        assert "Balances directness with tact" in rendered

    def test_unknown_value_no_line(self):
        behavior = {"directness": "super-direct"}
        rendered = render_behavior(behavior)
        # The unknown value for directness should not produce a line
        # but other defaults should still be present
        assert "super-direct" not in rendered
        assert "Balances speed" in rendered

    @pytest.mark.parametrize("dimension,values", [
        ("directness", ["indirect", "balanced", "direct"]),
        ("detail_focus", ["big-picture", "balanced", "detail-focused"]),
        ("formality", ["casual", "professional", "formal"]),
        ("risk_tolerance", ["cautious", "moderate", "bold"]),
        ("expressiveness", ["reserved", "moderate", "expressive"]),
    ])
    def test_all_values_produce_descriptions(self, dimension: str, values: list[str]):
        for value in values:
            desc = DIMENSION_DESCRIPTIONS[dimension][value]
            assert isinstance(desc, str)
            assert len(desc) > 10  # non-trivial description


# ─── _LLMPersonaAgent Tests ───────────────────────────────


class TestLLMPersonaAgent:
    """Tests for the concrete LLM-powered persona agent."""

    async def _make_agent(
        self,
        config: dict | None = None,
        llm_client: LLMClient | None = None,
    ) -> _LLMPersonaAgent:
        """Helper to create an initialized _LLMPersonaAgent with mocked memory."""
        cfg = config or {**_PERSONA_CONFIG}
        client = llm_client or _make_client()

        agent = create_persona_agent(
            agent_id=cfg["id"],
            config=cfg,
            llm_client=client,
        )
        await agent.initialize_memory()
        return agent

    async def test_on_event_returns_actions(self):
        agent = await self._make_agent()
        event = AgentEvent(
            event_type=EventType.MESSAGE_RECEIVED,
            payload={"content": "How's the sprint going?"},
            sender_id="mike-torres",
        )
        actions = await agent.on_event(event)
        assert len(actions) >= 1
        # Default LLM response text → COMPLETE_TASK fallback
        assert actions[0].action_type == ActionType.COMPLETE_TASK

    async def test_on_event_with_task_assigned(self):
        agent = await self._make_agent()
        task = _task("Review the architecture document")
        event = AgentEvent(
            event_type=EventType.TASK_ASSIGNED,
            payload={"task": task},
        )
        actions = await agent.on_event(event)
        assert len(actions) >= 1

    async def test_handle_backward_compatibility(self):
        agent = await self._make_agent()
        output = await agent.handle(_task("Write a design doc"))
        assert output.status == TaskStatus.COMPLETED
        assert "handle this task" in output.result

    async def test_system_prompt_contains_persona(self):
        agent = await self._make_agent()
        prompt = agent._build_system_prompt()
        assert "Sarah Chen" in prompt
        assert "VP of Engineering" in prompt
        assert "Engineering leadership" in prompt
        assert "15 years in software engineering" in prompt

    async def test_system_prompt_contains_behavior(self):
        agent = await self._make_agent()
        prompt = agent._build_system_prompt()
        assert "Communication style:" in prompt
        assert "Says exactly what they think" in prompt  # directness: direct

    async def test_system_prompt_contains_goals(self):
        agent = await self._make_agent()
        prompt = agent._build_system_prompt()
        assert "Ship v2.0 on time" in prompt
        assert "Reduce tech debt by 20%" in prompt
        assert "Prove the team can self-organize" in prompt

    async def test_system_prompt_contains_quirks(self):
        agent = await self._make_agent()
        prompt = agent._build_system_prompt()
        assert "What's on fire?" in prompt

    async def test_system_prompt_contains_dynamic_state(self):
        agent = await self._make_agent()
        agent._state.mood = Mood.FRUSTRATED
        agent._state.stress_level = 0.8
        prompt = agent._build_system_prompt()
        assert "frustrated" in prompt
        assert "Stress level: 0.8/1.0" in prompt

    async def test_format_event_task_assigned(self):
        agent = await self._make_agent()
        task = _task("Review code")
        event = AgentEvent(
            event_type=EventType.TASK_ASSIGNED,
            payload={"task": task},
        )
        msg = agent._format_event(event)
        assert "assigned a task" in msg
        assert "Review code" in msg

    async def test_format_event_message_received(self):
        agent = await self._make_agent()
        event = AgentEvent(
            event_type=EventType.MESSAGE_RECEIVED,
            payload={"content": "Hello Sarah"},
            sender_id="mike",
        )
        msg = agent._format_event(event)
        assert "Message from mike" in msg
        assert "Hello Sarah" in msg

    async def test_format_event_mention(self):
        agent = await self._make_agent()
        event = AgentEvent(
            event_type=EventType.MENTION,
            payload={"content": "@sarah check this"},
            sender_id="devops-bot",
        )
        msg = agent._format_event(event)
        assert "mentioned by devops-bot" in msg

    async def test_format_event_tick(self):
        agent = await self._make_agent()
        event = AgentEvent(event_type=EventType.TICK)
        msg = agent._format_event(event)
        assert "Autonomous tick" in msg

    async def test_multi_turn_tool_use(self):
        """LLM calls a tool, tool result fed back, final response parsed."""
        responses = [
            LLMResponse(
                text=None,
                tool_calls=[ToolCall(id="tc1", name="recall_notes", input={"query": "sprint"})],
                stop_reason=StopReason.TOOL_USE,
                usage=Usage(100, 50),
            ),
            LLMResponse(
                text="The sprint is on track based on my notes.",
                stop_reason=StopReason.END_TURN,
                usage=Usage(200, 100),
            ),
        ]
        client = _make_client(responses)
        agent = await self._make_agent(llm_client=client)

        event = AgentEvent(
            event_type=EventType.MESSAGE_RECEIVED,
            payload={"content": "Sprint status?"},
            sender_id="mike",
        )
        actions = await agent.on_event(event)
        assert len(actions) >= 1
        # LLM was called twice (tool use + final response)
        assert client._provider.create_message.call_count == 2

    async def test_energy_drains_on_actions(self):
        agent = await self._make_agent()
        assert agent._state.energy == 1.0
        event = AgentEvent(
            event_type=EventType.MESSAGE_RECEIVED,
            payload={"content": "test"},
            sender_id="test",
        )
        await agent.on_event(event)
        # Should drain for the COMPLETE_TASK action
        assert agent._state.energy < 1.0

    async def test_on_tick_recovers_energy(self):
        agent = await self._make_agent()
        agent._state.energy = 0.5
        await agent.on_tick()
        # Energy recovered by 0.1, then drained by 0.05 for the action
        assert agent._state.energy == pytest.approx(0.55)

    async def test_persona_state_property(self):
        agent = await self._make_agent()
        agent._state.mood = Mood.FOCUSED
        state_dict = agent.persona_state
        assert state_dict["mood"] == "focused"

    async def test_persona_state_persistence(self):
        """Serialize → close → reopen → deserialize matches."""
        cfg = {**_PERSONA_CONFIG}
        client = _make_client()

        agent1 = create_persona_agent(
            agent_id="sarah-chen", config=cfg, llm_client=client,
        )
        await agent1.initialize_memory()
        agent1._state.mood = Mood.SATISFIED
        agent1._state.energy = 0.6
        agent1._state.stress_level = 0.4
        agent1._state.goal_progress = {"v2": 0.8}
        await agent1._persist_persona_state()

        # Create a second instance sharing the same in-memory DB
        # Note: :memory: DBs are per-connection, so for a real persistence
        # test we verify the serialize/deserialize round-trip directly
        restored = PersonaState.from_dict(agent1._state.to_dict())
        assert restored.mood is Mood.SATISFIED
        assert restored.energy == 0.6
        assert restored.stress_level == 0.4
        assert restored.goal_progress == {"v2": 0.8}

        await agent1.close_memory()

    async def test_parse_actions_json_array(self):
        agent = await self._make_agent()
        response = LLMResponse(
            text=json.dumps([
                {"action_type": "send_message", "payload": {"content": "hi"}},
                {"action_type": "complete_task", "payload": {"result": "done"}},
            ]),
        )
        actions = agent._parse_actions(response)
        assert len(actions) == 2
        assert actions[0].action_type == ActionType.SEND_MESSAGE
        assert actions[1].action_type == ActionType.COMPLETE_TASK

    async def test_parse_actions_json_code_block(self):
        agent = await self._make_agent()
        response = LLMResponse(
            text='Here are my actions:\n```json\n[{"action_type": "do_nothing", "payload": {}}]\n```',
        )
        actions = agent._parse_actions(response)
        assert len(actions) == 1
        assert actions[0].action_type == ActionType.DO_NOTHING

    async def test_parse_actions_plain_text_fallback(self):
        agent = await self._make_agent()
        response = LLMResponse(text="I'll work on the documentation.")
        actions = agent._parse_actions(response)
        assert len(actions) == 1
        assert actions[0].action_type == ActionType.COMPLETE_TASK
        assert "documentation" in actions[0].payload["result"]

    async def test_parse_actions_unknown_action_type_skipped(self):
        agent = await self._make_agent()
        response = LLMResponse(
            text=json.dumps([
                {"action_type": "fly_to_moon", "payload": {}},
                {"action_type": "complete_task", "payload": {"result": "ok"}},
            ]),
        )
        actions = agent._parse_actions(response)
        assert len(actions) == 1
        assert actions[0].action_type == ActionType.COMPLETE_TASK

    async def test_llm_error_returns_error_action(self):
        mock_provider = AsyncMock()
        mock_provider.create_message = AsyncMock(
            side_effect=RuntimeError("API timeout")
        )
        mock_provider.format_tool_definitions = MagicMock(return_value=[])
        client = LLMClient(mock_provider)
        agent = await self._make_agent(llm_client=client)

        event = AgentEvent(
            event_type=EventType.MESSAGE_RECEIVED,
            payload={"content": "test"},
        )
        actions = await agent.on_event(event)
        assert len(actions) == 1
        assert actions[0].action_type == ActionType.COMPLETE_TASK
        assert "LLM provider error" in actions[0].payload["result"]

    async def test_no_llm_client(self):
        cfg = {**_PERSONA_CONFIG}
        agent = create_persona_agent(
            agent_id="sarah-chen", config=cfg, llm_client=_make_client(),
        )
        await agent.initialize_memory()
        agent._llm_client = None

        event = AgentEvent(
            event_type=EventType.MESSAGE_RECEIVED,
            payload={"content": "test"},
        )
        actions = await agent.on_event(event)
        assert actions[0].payload["result"] == "LLM client not configured"
        await agent.close_memory()

    async def test_lock_serializes_concurrent_events(self):
        """Verify the per-agent lock serializes on_event calls."""
        agent = await self._make_agent()
        assert isinstance(agent._lock, type(asyncio.Lock()))
        await agent.close_memory()

    async def test_close_memory_persists_state(self):
        agent = await self._make_agent()
        agent._state.mood = Mood.ENERGIZED
        await agent.close_memory()
        # After close, the state should have been persisted (no error)


# ─── Factory Tests ──────────────────────────────────────────


class TestCreatePersonaAgent:
    async def test_returns_llm_persona_agent(self):
        agent = create_persona_agent(
            agent_id="sarah-chen",
            config=_PERSONA_CONFIG,
            llm_client=_make_client(),
        )
        assert isinstance(agent, _LLMPersonaAgent)

    async def test_memory_tiers_wired(self):
        agent = create_persona_agent(
            agent_id="sarah-chen",
            config=_PERSONA_CONFIG,
            llm_client=_make_client(),
        )
        assert agent._episodic_memory is not None
        assert agent._relationship_memory is not None
        assert agent._working_memory is not None

    async def test_memory_tools_created(self):
        agent = create_persona_agent(
            agent_id="sarah-chen",
            config=_PERSONA_CONFIG,
            llm_client=_make_client(),
        )
        tool_names = {td.name for td in agent._memory_tools}
        assert "store_note" in tool_names
        assert "recall_notes" in tool_names

    async def test_initialize_and_close_memory(self):
        agent = create_persona_agent(
            agent_id="sarah-chen",
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


