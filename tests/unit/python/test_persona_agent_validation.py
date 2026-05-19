"""Tests for PersonaState data validation, tool registry, and sub-agent handling."""

import pytest

from agents.llm_client import ToolCall
from agents.persona import create_persona_agent
from agents.persona_runtime import _LLMPersonaAgent
from agents.persona_types import PersonaState, SubAgentRequest

from ._persona_test_helpers import _PERSONA_CONFIG, _make_client, _task

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

    def test_goal_progress_above_one_clamped(self):
        """Numeric value > 1.0 is clamped to 1.0 (e.g. corrupted DB row = 2.5).

        The clamping code at min(1.0, max(0.0, float(v))) handles this path but
        it was untested.  Without clamping, to_prompt_section() would render
        'goal: 250%', misleading the LLM.
        (PR review: numeric-out-of-range goal_progress path had zero test coverage.)
        """
        state = PersonaState.from_dict({"goal_progress": {"ship": 2.5}})
        assert state.goal_progress == {"ship": 1.0}

    def test_goal_progress_below_zero_clamped(self):
        """Numeric value < 0.0 is clamped to 0.0 (e.g. corrupted DB row = -0.5).

        (PR review: numeric-out-of-range goal_progress path had zero test coverage.)
        """
        state = PersonaState.from_dict({"goal_progress": {"debt": -0.5}})
        assert state.goal_progress == {"debt": 0.0}


# ─── Review finding: spawn_sub_agent without client ──────────


class TestSpawnSubAgentWithoutClient:
    """Verify spawn_sub_agent raises RuntimeError when orchestrator client is None.

    Review finding: this error path was untested.
    """

    async def test_raises_runtime_error(self):
        agent = create_persona_agent(
            agent_id="ember-owl",
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
        from agents.tools.builtin import ToolResult
        from agents.tools.registry import tool

        # Register a tool in the global registry with a unique name
        @tool(name="code_search", description="Search codebase")
        async def code_search(query: str) -> ToolResult:
            return ToolResult(success=True, data="results")

        cfg = {**_PERSONA_CONFIG, "tools": ["code_search"]}
        agent = create_persona_agent(
            agent_id="ember-owl", config=cfg, llm_client=_make_client(),
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
        from agents.tools.builtin import ToolResult
        from agents.tools.registry import tool

        @tool(name="store_note", description="WRONG: registry version")
        async def store_note_fake(text: str) -> ToolResult:
            return ToolResult(success=True, data="fake")

        cfg = {**_PERSONA_CONFIG, "tools": ["store_note"]}
        agent = create_persona_agent(
            agent_id="ember-owl", config=cfg, llm_client=_make_client(),
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
        from agents.tools.builtin import ToolResult
        from agents.tools.registry import tool

        @tool(name="secret_admin_tool", description="Should not be callable")
        async def secret_admin_tool() -> ToolResult:
            return ToolResult(success=True, data="should not reach here")

        # Agent config does NOT include "secret_admin_tool" in tools list
        cfg = {**_PERSONA_CONFIG, "tools": ["code_search"]}
        agent = create_persona_agent(
            agent_id="ember-owl", config=cfg, llm_client=_make_client(),
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
        from agents.tools.builtin import ToolResult
        from agents.tools.registry import tool

        @tool(name="allowed_tool", description="This one is allowed")
        async def allowed_tool() -> ToolResult:
            return ToolResult(success=True, data="executed")

        cfg = {**_PERSONA_CONFIG, "tools": ["allowed_tool"]}
        agent = create_persona_agent(
            agent_id="ember-owl", config=cfg, llm_client=_make_client(),
        )
        await agent.initialize_memory()

        results = await agent._execute_tools([
            ToolCall(id="tc1", name="allowed_tool", input={}),
        ])
        assert len(results) == 1
        assert results[0].is_error is False
        assert results[0].content == "executed"
        await agent.close_memory()

    async def test_execute_tools_returns_error_for_func_none_memory_tool(self):
        """_execute_tools must return is_error=True when a ToolDefinition has
        func=None (e.g. a schema-only declaration injected as a memory tool).

        The guard ``if tool_def is None or tool_def.func is None`` has two
        branches; only the first (tool_def is None) was covered by the
        test above.  A ToolDefinition with func=None can arise for schema
        documentation tools or if create_memory_tools() produces a stub
        entry.  (PR review: second branch of func=None guard untested.)
        """
        from agents.tools.registry import ToolDefinition

        null_func_tool = ToolDefinition(
            name="ghost",
            description="Schema-only declaration with no callable",
            parameters={},
            func=None,
        )
        agent = create_persona_agent(
            agent_id="ember-owl", config=_PERSONA_CONFIG, llm_client=_make_client(),
        )
        await agent.initialize_memory()
        # Inject the stub directly into the agent's memory-tools list so it
        # is found by the memory-tool-first lookup path.
        agent._memory_tools.append(null_func_tool)

        results = await agent._execute_tools([
            ToolCall(id="tc-ghost", name="ghost", input={}),
        ])

        assert len(results) == 1
        assert results[0].is_error is True
        assert "Unknown tool" in results[0].content
        await agent.close_memory()


# ─── Review finding: handle() without COMPLETE_TASK ──────────


class TestHandleWithoutCompleteTask:
    """Verify handle() returns FAILED when LLM produces no COMPLETE_TASK action.

    Review finding: the failure path in PersonaAgent.handle() was only
    implicitly tested via the default LLM response always producing COMPLETE_TASK.
    """

    async def test_handle_no_complete_task_returns_failed(self):
        import json

        from agents.base import TaskStatus
        from agents.llm_client import LLMResponse

        response = LLMResponse(text=json.dumps([
            {
                "action_type": "send_channel_message",
                "payload": {"content": "hi", "channel_id": "ch-1"},
            },
            {"action_type": "do_nothing", "payload": {}},
        ]))
        client = _make_client([response])
        agent = create_persona_agent(
            agent_id="ember-owl",
            config=_PERSONA_CONFIG,
            llm_client=client,
        )
        await agent.initialize_memory()

        output = await agent.handle(_task("do something"))
        assert output.status == TaskStatus.FAILED
        assert "No COMPLETE_TASK action taken" in output.result
        assert "send_channel_message" in output.result
        await agent.close_memory()
