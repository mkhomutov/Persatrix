"""
Tests for PersonaAgent runtime core — PersonaState, Mood enum,
behavioral dimension rendering, _LLMPersonaAgent, and create_persona_agent().

All tests use mock LLM client — no real API calls.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

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
    SubAgentRequest,
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
        "memory": {"read": True, "write": True},
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

    def test_unknown_dimension_ignored(self, caplog):
        behavior = {"unknown_dim": "unknown_val"}
        with caplog.at_level("WARNING", logger="agents.persona"):
            rendered = render_behavior(behavior)
        # Should still have defaults for known dimensions
        assert "Balances directness with tact" in rendered
        # PR #54 review: unknown dimensions now emit a warning
        assert "Unknown behavior dimension" in caplog.text
        assert "unknown_dim" in caplog.text

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
        """Serialize → persist to DB → load from DB → values match."""
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

        # Load back from the actual DB via _load_persona_state()
        # (same agent / same DB connection — exercises the full DB path)
        restored = await agent1._load_persona_state()
        assert restored.mood is Mood.SATISFIED
        assert restored.energy == 0.6
        assert restored.stress_level == 0.4
        assert restored.goal_progress == {"v2": 0.8}

        await agent1.close_memory()

    async def test_parse_actions_json_array(self):
        agent = await self._make_agent()
        response = LLMResponse(
            text=json.dumps([
                {"action_type": "send_message", "payload": {"channel_id": "general", "content": "hi"}},
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
        """Verify the per-agent lock actually serializes concurrent on_event calls.

        Uses asyncio.gather to run two events concurrently. A shared list
        records enter/exit markers — if the lock works, one event fully
        completes before the other starts (no interleaving).

        Review finding: previous test only checked isinstance() which is
        a no-op verification.
        """
        agent = await self._make_agent()
        order: list[str] = []
        original_inner = agent._on_event_inner

        async def _tracking_inner(event: AgentEvent) -> list:
            label = event.payload.get("label", "?")
            order.append(f"enter-{label}")
            result = await original_inner(event)
            order.append(f"exit-{label}")
            return result

        agent._on_event_inner = _tracking_inner  # type: ignore[assignment]

        e1 = AgentEvent(
            event_type=EventType.MESSAGE_RECEIVED,
            payload={"content": "first", "label": "1"},
            sender_id="a",
        )
        e2 = AgentEvent(
            event_type=EventType.MESSAGE_RECEIVED,
            payload={"content": "second", "label": "2"},
            sender_id="b",
        )
        await asyncio.gather(agent.on_event(e1), agent.on_event(e2))

        # With proper lock serialization, events must not interleave:
        # either [enter-1, exit-1, enter-2, exit-2] or [enter-2, exit-2, enter-1, exit-1]
        assert order[0].startswith("enter-")
        assert order[1].startswith("exit-")
        assert order[0][-1] == order[1][-1]  # same label = same event
        assert order[2].startswith("enter-")
        assert order[3].startswith("exit-")
        assert order[2][-1] == order[3][-1]
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

    async def test_working_memory_not_conflated_with_llm_max_tokens(self):
        """F-5a-1: config['max_tokens'] is the LLM completion limit, not the
        working memory budget. Working memory should read from
        memory.working.max_tokens instead."""
        config = {
            **_PERSONA_CONFIG,
            "max_tokens": 4096,  # LLM completion limit — must NOT affect working memory
        }
        agent = create_persona_agent(
            agent_id="sarah-chen",
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
            agent_id="sarah-chen",
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


# ─── Review follow-up: additional coverage ──────────────────


class TestFormatEventAdditional:
    """Tests for _format_event() event types not covered above (review finding #7)."""

    async def _make_agent(self) -> _LLMPersonaAgent:
        agent = create_persona_agent(
            agent_id="sarah-chen",
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
            agent_id="sarah-chen", config=config, llm_client=client,
        )
        await agent.initialize_memory()

        event = AgentEvent(
            event_type=EventType.MESSAGE_RECEIVED,
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


# ─── Review follow-up: convenience method tests ─────────────


class TestConvenienceMethods:
    """Tests for PersonaAgent.message(), complete(), delegate_to() (review finding).

    These action constructors are part of the public API. Verifying their
    structure ensures downstream action executors receive correct payloads.
    """

    async def _make_agent(self) -> _LLMPersonaAgent:
        agent = create_persona_agent(
            agent_id="sarah-chen",
            config=_PERSONA_CONFIG,
            llm_client=_make_client(),
        )
        await agent.initialize_memory()
        return agent

    async def test_message_action(self):
        agent = await self._make_agent()
        action = agent.message("ch-1", "Hello team", mentions=["mike"])
        assert action.action_type == ActionType.SEND_MESSAGE
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


# ─── Review follow-up: from_dict clamping tests ─────────────


class TestPersonaStateFromDictClamping:
    """Verify from_dict clamps out-of-range values from corrupted data.

    Review finding: corrupted DB data (energy: 5.0, stress: -1.0) would
    produce broken prompt sections like 'Energy level: 5.0/1.0'.
    """

    def test_energy_clamped_above_one(self):
        state = PersonaState.from_dict({"energy": 5.0})
        assert state.energy == 1.0

    def test_energy_clamped_below_zero(self):
        state = PersonaState.from_dict({"energy": -3.0})
        assert state.energy == 0.0

    def test_stress_clamped_above_one(self):
        state = PersonaState.from_dict({"stress_level": 999.0})
        assert state.stress_level == 1.0

    def test_stress_clamped_below_zero(self):
        state = PersonaState.from_dict({"stress_level": -1.0})
        assert state.stress_level == 0.0

    def test_normal_values_unchanged(self):
        state = PersonaState.from_dict({"energy": 0.7, "stress_level": 0.3})
        assert state.energy == 0.7
        assert state.stress_level == 0.3


# ─── Review finding: goal_progress validation ────────────────


class TestPersonaStateGoalProgressValidation:
    """Verify from_dict rejects non-numeric goal_progress values.

    Review finding: corrupted DB entry with {"goal": "not_a_number"}
    would crash to_prompt_section() at the f"{progress:.0%}" format string.
    """

    def test_valid_goal_progress_preserved(self):
        state = PersonaState.from_dict({"goal_progress": {"ship": 0.75, "hire": 0.5}})
        assert state.goal_progress == {"ship": 0.75, "hire": 0.5}

    def test_non_numeric_value_skipped(self):
        state = PersonaState.from_dict({"goal_progress": {"ship": "not_a_number"}})
        assert state.goal_progress == {}

    def test_mixed_valid_invalid_values(self):
        state = PersonaState.from_dict(
            {"goal_progress": {"ship": 0.75, "bad": None, "hire": "oops"}}
        )
        assert state.goal_progress == {"ship": 0.75}

    def test_integer_coerced_to_float(self):
        state = PersonaState.from_dict({"goal_progress": {"done": 1}})
        assert state.goal_progress == {"done": 1.0}

    def test_empty_goal_progress(self):
        state = PersonaState.from_dict({"goal_progress": {}})
        assert state.goal_progress == {}

    def test_prompt_section_after_validation(self):
        """Validated goal_progress renders without error."""
        state = PersonaState.from_dict({"goal_progress": {"ship": 0.75}})
        section = state.to_prompt_section()
        assert "ship: 75%" in section


# ─── Review finding: spawn_sub_agent without client ──────────


class TestSpawnSubAgentWithoutClient:
    """Verify spawn_sub_agent raises RuntimeError when orchestrator client is None.

    Review finding: this error path was untested.
    """

    async def test_raises_runtime_error(self):
        agent = create_persona_agent(
            agent_id="sarah-chen",
            config=_PERSONA_CONFIG,
            llm_client=_make_client(),
        )
        await agent.initialize_memory()
        assert agent._orchestrator_client is None

        with pytest.raises(RuntimeError, match="Orchestrator client not initialized"):
            await agent.spawn_sub_agent(SubAgentRequest(role="helper", task="test"))

        await agent.close_memory()


# ─── Review finding: tool precedence (registry + memory) ────


class TestBuildToolDefinitionsWithRegistry:
    """Verify _build_tool_definitions includes registry tools and memory tools
    take precedence over same-name registry tools.

    Review finding: only memory tools were tested; tool precedence logic was
    not verified.
    """

    async def _make_agent(self) -> _LLMPersonaAgent:
        from agents.tools.registry import tool
        from agents.tools.builtin import ToolResult

        # Register a tool in the global registry with a unique name
        @tool(name="code_search", description="Search codebase")
        async def code_search(query: str) -> ToolResult:
            return ToolResult(success=True, data="results")

        cfg = {**_PERSONA_CONFIG, "tools": ["code_search"]}
        agent = create_persona_agent(
            agent_id="sarah-chen", config=cfg, llm_client=_make_client(),
        )
        await agent.initialize_memory()
        return agent

    async def test_registry_tool_included(self):
        agent = await self._make_agent()
        defs = agent._build_tool_definitions()
        names = {d["name"] for d in defs}
        assert "code_search" in names
        # Memory tools should also be present
        assert "store_note" in names
        assert "recall_notes" in names
        await agent.close_memory()

    async def test_memory_tool_overrides_registry_tool(self):
        """If a registry tool has the same name as a memory tool, memory wins."""
        from agents.tools.registry import tool
        from agents.tools.builtin import ToolResult

        @tool(name="store_note", description="WRONG: registry version")
        async def store_note_fake(text: str) -> ToolResult:
            return ToolResult(success=True, data="fake")

        cfg = {**_PERSONA_CONFIG, "tools": ["store_note"]}
        agent = create_persona_agent(
            agent_id="sarah-chen", config=cfg, llm_client=_make_client(),
        )
        await agent.initialize_memory()

        defs = agent._build_tool_definitions()
        store_defs = [d for d in defs if d["name"] == "store_note"]
        assert len(store_defs) == 1
        # The description should be from the memory tool, not the registry fake
        assert "WRONG" not in store_defs[0]["description"]
        await agent.close_memory()

    async def test_execute_tools_rejects_unlisted_registry_tool(self):
        """F-5a-2: _execute_tools must not invoke a registry tool that is not
        in the agent's config['tools'] list, even if the tool exists in the
        global registry.  Defense-in-depth against LLM-hallucinated tool names."""
        from agents.tools.registry import tool
        from agents.tools.builtin import ToolResult

        @tool(name="secret_admin_tool", description="Should not be callable")
        async def secret_admin_tool() -> ToolResult:
            return ToolResult(success=True, data="should not reach here")

        # Agent config does NOT include "secret_admin_tool" in tools list
        cfg = {**_PERSONA_CONFIG, "tools": ["code_search"]}
        agent = create_persona_agent(
            agent_id="sarah-chen", config=cfg, llm_client=_make_client(),
        )
        await agent.initialize_memory()

        results = await agent._execute_tools([
            ToolCall(id="tc1", name="secret_admin_tool", input={}),
        ])
        assert len(results) == 1
        assert results[0].is_error is True
        assert "Unknown tool" in results[0].content
        await agent.close_memory()

    async def test_execute_tools_allows_listed_registry_tool(self):
        """F-5a-2: Registry tools that ARE in config['tools'] should execute normally."""
        from agents.tools.registry import tool
        from agents.tools.builtin import ToolResult

        @tool(name="allowed_tool", description="This one is allowed")
        async def allowed_tool() -> ToolResult:
            return ToolResult(success=True, data="executed")

        cfg = {**_PERSONA_CONFIG, "tools": ["allowed_tool"]}
        agent = create_persona_agent(
            agent_id="sarah-chen", config=cfg, llm_client=_make_client(),
        )
        await agent.initialize_memory()

        results = await agent._execute_tools([
            ToolCall(id="tc1", name="allowed_tool", input={}),
        ])
        assert len(results) == 1
        assert results[0].is_error is False
        assert results[0].content == "executed"
        await agent.close_memory()


# ─── Review finding: handle() without COMPLETE_TASK ──────────


class TestHandleWithoutCompleteTask:
    """Verify handle() returns FAILED when LLM produces no COMPLETE_TASK action.

    Review finding: the failure path in PersonaAgent.handle() was only
    implicitly tested via the default LLM response always producing COMPLETE_TASK.
    """

    async def test_handle_no_complete_task_returns_failed(self):
        response = LLMResponse(
            text=json.dumps([
                {"action_type": "send_message", "payload": {"content": "hi", "channel_id": "ch-1"}},
                {"action_type": "do_nothing", "payload": {}},
            ]),
        )
        client = _make_client([response])
        agent = create_persona_agent(
            agent_id="sarah-chen",
            config=_PERSONA_CONFIG,
            llm_client=client,
        )
        await agent.initialize_memory()

        output = await agent.handle(_task("do something"))
        assert output.status == TaskStatus.FAILED
        assert "No COMPLETE_TASK action taken" in output.result
        assert "send_message" in output.result
        await agent.close_memory()


# ─── PR #54 review: close_memory() partial tier failure ──────


class TestCloseMemoryPartialFailure:
    """Verify close_memory() closes all tiers even if one raises.

    PR #54 review finding #3: sequential close without try/finally meant a
    failure in an earlier tier would leak later tiers' DB connections.
    """

    async def test_later_tiers_closed_when_earlier_tier_raises(self):
        """If episodic close() raises, relationship memory is still closed."""
        agent = create_persona_agent(
            agent_id="sarah-chen",
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
            agent_id="sarah-chen",
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
            agent_id="sarah-chen",
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
            agent_id="sarah-chen",
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
            agent_id="sarah-chen",
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
            agent_id="sarah-chen",
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

    async def _make_agent(self) -> _LLMPersonaAgent:
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
            agent_id="sarah-chen", config=config, llm_client=client,
        )
        await agent.initialize_memory()

        event = AgentEvent(
            event_type=EventType.MESSAGE_RECEIVED,
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
            agent_id="sarah-chen", config=config, llm_client=_make_client(),
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
            agent_id="sarah-chen", config=config, llm_client=client,
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
            agent_id="sarah-chen", config=config, llm_client=_make_client(),
        )
        await agent.initialize_memory()

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
            agent_id="sarah-chen", config=config, llm_client=client,
        )
        await agent.initialize_memory()

        # Set energy to a known value
        agent._state.energy = 0.5
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
            agent_id="sarah-chen", config=config, llm_client=_make_client(),
        )
        await agent.initialize_memory()

        agent._state.energy = 0.5
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


# ─── F-5b-1: _inject_memory_context ───────────────────────

class TestInjectMemoryContext:
    """F-5b-1: Memory context injection into working memory."""

    async def test_injects_episodic_and_notes(self):
        """_inject_memory_context adds episodic and note sections."""
        agent = create_persona_agent(
            agent_id="sarah-chen", config=_PERSONA_CONFIG, llm_client=_make_client(),
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
            agent_id="sarah-chen", config=_PERSONA_CONFIG, llm_client=_make_client(),
        )
        await agent.initialize_memory()

        # Record an interaction to create a relationship.
        await agent._relationship_memory.record_interaction(
            other_agent_id="mike-torres",
            interaction_type="collaboration",
            outcome="success",
            sentiment=0.8,
        )

        event = AgentEvent(
            event_type=EventType.MESSAGE_RECEIVED,
            payload={"content": "Hello"},
            sender_id="mike-torres",
        )
        await agent._inject_memory_context(event)

        rel_section = agent._working_memory.get_section("relationship_context")
        assert rel_section is not None
        assert "mike-torres" in rel_section.content
        assert rel_section.priority == 8
        await agent.close_memory()

    async def test_no_sender_skips_relationship(self):
        """_inject_memory_context skips relationship when no sender_id."""
        agent = create_persona_agent(
            agent_id="sarah-chen", config=_PERSONA_CONFIG, llm_client=_make_client(),
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
            agent_id="sarah-chen", config=_PERSONA_CONFIG, llm_client=_make_client(),
        )
        await agent.initialize_memory()

        # Sabotage recall to simulate failure.
        agent._episodic_memory.recall = AsyncMock(side_effect=RuntimeError("db locked"))
        agent._episodic_memory.recall_notes = AsyncMock(side_effect=RuntimeError("db locked"))

        event = AgentEvent(
            event_type=EventType.MESSAGE_RECEIVED,
            payload={"content": "test"},
            sender_id="mike-torres",
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
            agent_id="sarah-chen", config=_PERSONA_CONFIG, llm_client=_make_client(),
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
            sender_id="mike-torres",
        )
        # Should not raise — all tiers fail gracefully.
        await agent._inject_memory_context(event)

        # No sections should be present.
        assert agent._working_memory.get_section("episodic_recall") is None
        assert agent._working_memory.get_section("relationship_context") is None
        assert agent._working_memory.get_section("recent_notes") is None
        await agent.close_memory()

    async def test_tick_skips_episodic_recall(self):
        """TICK events skip episodic recall to avoid low-signal FTS5 matches.

        The boilerplate "Autonomous tick: review your goals..." query would
        match broadly in FTS5, wasting I/O.  Notes recall is still attempted
        (notes contain the agent's personal knowledge relevant for autonomous
        goal review), though results depend on FTS5/LIKE matching.
        (PR #60 review: TICK events waste I/O on low-signal FTS5 matches.)
        """
        agent = create_persona_agent(
            agent_id="sarah-chen", config=_PERSONA_CONFIG, llm_client=_make_client(),
        )
        await agent.initialize_memory()

        # Store an episode so recall would return results if called.
        await agent._episodic_memory.store_episode(
            summary="Previous architecture discussion",
            context={"topic": "arch"},
            importance=0.8,
        )

        # Spy on recall to verify it's NOT called for TICK.
        recall_spy = AsyncMock(wraps=agent._episodic_memory.recall)
        agent._episodic_memory.recall = recall_spy

        # Spy on recall_notes to verify it IS still called.
        notes_spy = AsyncMock(wraps=agent._episodic_memory.recall_notes)
        agent._episodic_memory.recall_notes = notes_spy

        event = AgentEvent(event_type=EventType.TICK, payload={})
        await agent._inject_memory_context(event)

        # Episodic recall should NOT be called for TICK events.
        recall_spy.assert_not_called()
        # Notes recall should still be attempted.
        notes_spy.assert_called_once()

        # No episodic section injected.
        assert agent._working_memory.get_section("episodic_recall") is None
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
            agent_id="sarah-chen", config=_PERSONA_CONFIG, llm_client=_make_client(),
        )
        await agent.initialize_memory()

        # Bootstrap a relationship with trust but zero interactions.
        await agent._relationship_memory.update_trust(
            other_agent_id="mike-torres",
            delta=0.1,
            reason="config bootstrap",
        )

        event = AgentEvent(
            event_type=EventType.MESSAGE_RECEIVED,
            payload={"content": "Hello"},
            sender_id="mike-torres",
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
            agent_id="sarah-chen", config=_PERSONA_CONFIG, llm_client=_make_client(),
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
            agent_id="sarah-chen", config=_PERSONA_CONFIG, llm_client=_make_client(),
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
            agent_id="sarah-chen", config=_PERSONA_CONFIG, llm_client=_make_client(),
        )
        await agent.initialize_memory()

        # Record interaction without sentiment to keep trust at ~0.5.
        await agent._relationship_memory.record_interaction(
            other_agent_id="mike-torres",
            interaction_type="collaboration",
            outcome="neutral",
            sentiment=0.0,
        )

        event = AgentEvent(
            event_type=EventType.MESSAGE_RECEIVED,
            payload={"content": "Hello"},
            sender_id="mike-torres",
        )
        await agent._inject_memory_context(event)

        rel_section = agent._working_memory.get_section("relationship_context")
        assert rel_section is not None
        # Trust line should NOT appear when trust is ~0.5 default.
        assert "Trust:" not in rel_section.content
        # Interaction count should still appear.
        assert "Interactions:" in rel_section.content
        await agent.close_memory()

    async def test_relationship_notes_truncated(self):
        """F-60-5: relationship notes exceeding 300 chars are truncated."""
        agent = create_persona_agent(
            agent_id="sarah-chen", config=_PERSONA_CONFIG, llm_client=_make_client(),
        )
        await agent.initialize_memory()

        # Record interaction to create a relationship with long notes.
        await agent._relationship_memory.record_interaction(
            other_agent_id="mike-torres",
            interaction_type="collaboration",
            outcome="success",
            sentiment=0.8,
        )
        # Manually set long notes on the relationship.
        long_notes = "n" * 600
        async with agent._relationship_memory._db.execute(
            "UPDATE relationships SET notes = ? WHERE agent_id = ? AND other_agent_id = ?",
            (long_notes, "sarah-chen", "mike-torres"),
        ):
            pass
        await agent._relationship_memory._db.commit()

        event = AgentEvent(
            event_type=EventType.MESSAGE_RECEIVED,
            payload={"content": "Hello"},
            sender_id="mike-torres",
        )
        await agent._inject_memory_context(event)

        rel_section = agent._working_memory.get_section("relationship_context")
        assert rel_section is not None
        # Full 600-char notes should NOT appear — capped at 300.
        assert long_notes not in rel_section.content
        assert "n" * 300 in rel_section.content
        await agent.close_memory()

