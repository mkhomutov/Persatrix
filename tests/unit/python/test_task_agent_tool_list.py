"""A task agent runs only the tools on its ``tools`` list (ISSUE-0151).

Each agent in ``config/agents.yaml`` lists the tools it may use.
``_build_tool_definitions`` has always offered a task agent only the
registered tools on that list, but ``_execute_tools`` ran any registered
tool the model named, listed or not — while a persona refuses such a call
(``agents/persona_runtime/action_loop.py``). These tests pin the task-agent
rule: a call to a tool the agent was not offered gets the same
``Unknown tool`` error as a tool that does not exist, and never runs. Both
methods share the rule in :mod:`agents.tools.tool_list`.

``TestExecuteTools`` and ``TestBuildToolDefinitions`` moved here from
``test_base_handle.py``, which sat past the 485-line split point; each
``TestExecuteTools`` agent now lists the tool it runs.

The model is mocked at the ``LLMClient`` boundary: a real
:class:`LLMClient` over a mocked provider. No network calls.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.base import BaseAgent, TaskInput, TaskInputConfig, TaskOutput, TaskStatus
from agents.llm_client import (
    LLMClient,
    LLMResponse,
    LLMToolResult,
    StopReason,
    ToolCall,
    Usage,
)
from agents.memory.episodic import EpisodicMemory
from agents.task_agent import TaskAgent
from agents.tools.memory_tools import create_memory_tools
from agents.tools.permissions import PermissionGate
from agents.tools.registry import ToolResult, get_tool, tool


class _TestableAgent(BaseAgent):
    """A bare BaseAgent for the moved offer and run tests; no LLM loop runs."""

    async def handle(self, task: TaskInput) -> TaskOutput:  # pragma: no cover
        raise NotImplementedError


def _register(name: str, ran: list[str]) -> None:
    """Register a no-argument tool called *name* that records each run."""

    @tool(name=name, description=f"{name} stub")
    async def _stub() -> ToolResult:
        ran.append(name)
        return ToolResult(success=True, data=f"{name} ran")


def _client(
    responses: list[LLMResponse],
) -> tuple[LLMClient, AsyncMock, list[list[LLMToolResult]]]:
    """A real LLMClient over a mocked provider, the provider, and its tool rounds.

    ``format_tool_definitions`` passes the offered definitions through, so a
    test can read what the model was offered off the provider's call args.
    """
    rounds: list[list[LLMToolResult]] = []

    def _append(
        msgs: list[dict[str, Any]], _resp: LLMResponse, results: list[LLMToolResult],
    ) -> list[dict[str, Any]]:
        rounds.append(list(results))
        return [*msgs, {"role": "assistant", "content": "tool round"},
                {"role": "user", "content": "tool results"}]

    provider = AsyncMock()
    provider.create_message = AsyncMock(side_effect=responses)
    provider.format_tool_definitions = MagicMock(side_effect=lambda defs: defs)
    provider.append_tool_round = MagicMock(side_effect=_append)
    return LLMClient(provider), provider, rounds


def _tool_use(*names: str) -> LLMResponse:
    return LLMResponse(
        text=None,
        tool_calls=[ToolCall(id=f"tc{i}", name=n, input={}) for i, n in enumerate(names)],
        stop_reason=StopReason.TOOL_USE,
        usage=Usage(10, 10),
    )


def _end(text: str = "done") -> LLMResponse:
    return LLMResponse(text=text, stop_reason=StopReason.END_TURN, usage=Usage(10, 10))


def _task() -> TaskInput:
    return TaskInput(
        task_id="t1", workflow_id="w1", payload="review this", config=TaskInputConfig(),
    )


class TestUnlistedToolRefused:
    async def test_unlisted_registered_tool_is_refused_and_not_run(self) -> None:
        ran: list[str] = []
        _register("listed", ran)
        _register("unlisted", ran)
        agent = TaskAgent(agent_id="code-reviewer", config={"tools": ["listed"]})

        results = await agent._execute_tools([
            ToolCall(id="c1", name="unlisted", input={}),
            ToolCall(id="c2", name="listed", input={}),
        ])

        assert [(r.content, r.is_error) for r in results] == [
            ("Unknown tool: unlisted", True),
            ("listed ran", False),
        ]
        assert ran == ["listed"]

    async def test_refusal_reads_like_a_tool_that_does_not_exist(self) -> None:
        # The model must learn nothing about tools it was not given, so a
        # registered-but-unlisted tool and a missing one get the same answer.
        _register("hidden", [])
        agent = TaskAgent(agent_id="planner", config={"tools": []})

        hidden, ghost = await agent._execute_tools([
            ToolCall(id="c1", name="hidden", input={}),
            ToolCall(id="c2", name="ghost", input={}),
        ])

        assert (hidden.content, hidden.is_error) == ("Unknown tool: hidden", True)
        assert (ghost.content, ghost.is_error) == ("Unknown tool: ghost", True)

    async def test_persona_note_tool_in_the_same_process_is_refused(
        self, memory: EpisodicMemory,
    ) -> None:
        # A persona's note tools register in the one global registry, bound to
        # that persona's memory; a task agent must not reach them by name.
        create_memory_tools(memory, PermissionGate({"memory": {"read": True, "write": True}}))
        agent = TaskAgent(agent_id="planner", config={"tools": []})

        [result] = await agent._execute_tools([
            ToolCall(id="c1", name="store_note", input={"topic": "t", "content": "c"}),
        ])

        assert (result.content, result.is_error) == ("Unknown tool: store_note", True)
        assert await memory.count_notes() == 0
        # Vacuous-pass guard: the same tool, run directly, does write.
        store_note = get_tool("store_note")
        assert store_note is not None and store_note.func is not None
        await store_note.func(topic="t", content="c")
        assert await memory.count_notes() == 1


class TestToolListShapes:
    @pytest.mark.parametrize(
        "config",
        [
            {},
            {"tools": []},
            {"tools": None},
            {"tools": "listed"},
            {"tools": [{"name": "listed"}, 7]},
        ],
        ids=["no-key", "empty", "no-value", "scalar", "non-strings"],
    )
    async def test_no_usable_list_offers_and_runs_nothing(
        self, config: dict[str, Any],
    ) -> None:
        ran: list[str] = []
        _register("listed", ran)
        agent = TaskAgent(agent_id="t", config=config)

        assert agent._build_tool_definitions() == []
        [result] = await agent._execute_tools([ToolCall(id="c1", name="listed", input={})])

        assert (result.content, result.is_error) == ("Unknown tool: listed", True)
        assert ran == []

    async def test_non_string_entries_are_skipped_not_fatal(self) -> None:
        ran: list[str] = []
        _register("listed", ran)
        agent = TaskAgent(agent_id="t", config={"tools": [{"name": "x"}, "listed", 7]})

        assert [d["name"] for d in agent._build_tool_definitions()] == ["listed"]
        [result] = await agent._execute_tools([ToolCall(id="c1", name="listed", input={})])

        assert (result.content, result.is_error) == ("listed ran", False)
        assert ran == ["listed"]


class TestOfferMatchesRun:
    async def test_the_tools_run_are_exactly_the_tools_offered(self) -> None:
        ran: list[str] = []
        for name in ("file_read", "file_write", "shell_exec"):
            _register(name, ran)
        # An `mcp:` entry and a typo give no tool (ISSUE-0147); file_write is
        # registered but not listed.
        agent = TaskAgent(
            agent_id="t",
            config={"tools": ["file_read", "mcp:github", "file_raed", "shell_exec"]},
        )
        names = ["file_read", "file_write", "shell_exec", "mcp:github", "file_raed"]

        offered = {d["name"] for d in agent._build_tool_definitions()}
        results = await agent._execute_tools(
            [ToolCall(id=f"c{i}", name=n, input={}) for i, n in enumerate(names)],
        )

        assert offered == {"file_read", "shell_exec"}
        assert set(ran) == offered
        refused = {n for n, r in zip(names, results, strict=True) if r.is_error}
        assert refused == {"file_write", "mcp:github", "file_raed"}


class TestThroughHandle:
    async def test_unlisted_call_goes_back_to_the_model_and_the_task_completes(
        self,
    ) -> None:
        ran: list[str] = []
        _register("listed", ran)
        _register("unlisted", ran)
        client, provider, rounds = _client([_tool_use("unlisted"), _end()])
        agent = TaskAgent(
            agent_id="code-reviewer",
            config={"model": "test-model", "role": "Reviewer", "tools": ["listed"]},
            llm_client=client,
        )

        output = await agent.handle(_task())

        assert output.status == TaskStatus.COMPLETED
        assert output.result == "done"
        offered = provider.format_tool_definitions.call_args.args[0]
        assert [d["name"] for d in offered] == ["listed"]
        assert [(r.content, r.is_error) for r in rounds[0]] == [
            ("Unknown tool: unlisted", True),
        ]
        assert ran == []

    async def test_memory_enabled_task_agent_keeps_memories_and_listed_tools(
        self,
    ) -> None:
        # RFC 0008: a task agent's memory reaches the model through the system
        # prompt, not through tools, so the list rule must leave it untouched.
        ran: list[str] = []
        _register("listed", ran)
        client, provider, rounds = _client([_tool_use("listed"), _end()])
        agent = TaskAgent(
            agent_id="code-writer",
            config={
                "model": "test-model", "role": "Writer", "tools": ["listed"],
                "memory": {"enabled": True},
            },
            llm_client=client,
        )
        store = MagicMock()
        store.retrieve_relevant = AsyncMock(
            return_value=[SimpleNamespace(content="Deploys freeze on Fridays")],
        )
        agent._memory = store

        output = await agent.handle(_task())

        assert output.status == TaskStatus.COMPLETED
        assert ran == ["listed"]
        assert [(r.content, r.is_error) for r in rounds[0]] == [("listed ran", False)]
        system = provider.create_message.call_args_list[0].kwargs["system"]
        assert "Deploys freeze on Fridays" in system


# ─── _execute_tools (moved from test_base_handle.py) ────────


class TestExecuteTools:
    async def test_successful_tool(self):
        @tool(name="good_tool", description="Works")
        async def good_tool(x: str) -> ToolResult:
            return ToolResult(success=True, data=f"result: {x}")

        agent = _TestableAgent(agent_id="t", config={"tools": ["good_tool"]})
        results = await agent._execute_tools(
            [ToolCall(id="c1", name="good_tool", input={"x": "hello"})]
        )
        assert len(results) == 1
        assert results[0].content == "result: hello"
        assert results[0].is_error is False

    async def test_failed_tool(self):
        @tool(name="bad_tool", description="Error")
        async def bad_tool() -> ToolResult:
            return ToolResult(success=False, error="something broke")

        agent = _TestableAgent(agent_id="t", config={"tools": ["bad_tool"]})
        results = await agent._execute_tools(
            [ToolCall(id="c1", name="bad_tool", input={})]
        )
        assert results[0].content == "something broke"
        assert results[0].is_error is True

    async def test_unknown_tool(self):
        agent = _TestableAgent(agent_id="t", config={})
        results = await agent._execute_tools(
            [ToolCall(id="c1", name="nonexistent", input={})]
        )
        assert results[0].is_error is True
        assert "Unknown tool" in results[0].content

    async def test_permission_error_caught(self):
        @tool(name="perm_tool", description="Perm check")
        async def perm_tool() -> ToolResult:
            raise PermissionError("denied")

        agent = _TestableAgent(agent_id="t", config={"tools": ["perm_tool"]})
        results = await agent._execute_tools(
            [ToolCall(id="c1", name="perm_tool", input={})]
        )
        assert results[0].is_error is True
        assert "denied" in results[0].content

    async def test_generic_exception_caught(self):
        @tool(name="crash_tool", description="Crashes")
        async def crash_tool() -> ToolResult:
            raise ValueError("boom")

        agent = _TestableAgent(agent_id="t", config={"tools": ["crash_tool"]})
        results = await agent._execute_tools(
            [ToolCall(id="c1", name="crash_tool", input={})]
        )
        assert results[0].is_error is True
        assert "ValueError" in results[0].content
        assert "boom" in results[0].content

    async def test_multiple_tools_sequential(self):
        call_order: list[str] = []

        @tool(name="tool_a", description="A")
        async def tool_a() -> ToolResult:
            call_order.append("a")
            return ToolResult(success=True, data="a")

        @tool(name="tool_b", description="B")
        async def tool_b() -> ToolResult:
            call_order.append("b")
            return ToolResult(success=True, data="b")

        agent = _TestableAgent(agent_id="t", config={"tools": ["tool_a", "tool_b"]})
        results = await agent._execute_tools([
            ToolCall(id="c1", name="tool_a", input={}),
            ToolCall(id="c2", name="tool_b", input={}),
        ])
        assert len(results) == 2
        assert call_order == ["a", "b"]


# ─── Build Tool Definitions (moved from test_base_handle.py) ─


class TestBuildToolDefinitions:
    def test_builds_from_registry(self):
        @tool(name="my_tool", description="My tool")
        async def my_tool(path: str) -> ToolResult:
            return ToolResult(success=True, data="ok")

        # S-12: agent must have the tool in its config to expose it
        agent = _TestableAgent(agent_id="t", config={"tools": ["my_tool"]})
        defs = agent._build_tool_definitions()
        assert len(defs) == 1
        assert defs[0]["name"] == "my_tool"
        assert defs[0]["description"] == "My tool"
        assert "path" in defs[0]["parameters"]["properties"]

    def test_empty_registry(self):
        agent = _TestableAgent(agent_id="t", config={})
        defs = agent._build_tool_definitions()
        assert defs == []

    def test_empty_tools_config_exposes_nothing(self):
        """S-12: empty tools list means no tools exposed."""
        @tool(name="hidden_tool", description="Should not appear")
        async def hidden_tool() -> ToolResult:
            return ToolResult(success=True, data="ok")

        agent = _TestableAgent(agent_id="t", config={"tools": []})
        defs = agent._build_tool_definitions()
        assert defs == []
