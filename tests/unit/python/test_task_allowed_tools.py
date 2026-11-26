"""A task's ``allowed_tools`` list narrows the tools its agent offers and runs (ISSUE-0214).

``TaskConfig.allowed_tools`` was copied off the wire and never read: a task
that named one tool still ran every tool on its agent's list. Both agent
kinds now hold a task to its list, by one rule in
:mod:`agents.tools.tool_list`: a tool the list does not name is not offered,
and a call to it gets the ``Unknown tool`` answer and does not run.

An empty list is no restriction from this field, as it has to be: the wire
cannot tell an empty list from an unset one, and the orchestrator never
sets it. The list only narrows: it cannot give an agent a tool its own
``tools`` list does not.

Every "not offered" assertion here compares the whole offered list, and has
a control that offers more: the unit suite empties the tool registry before
each test, so a bare "is not offered" would pass for an empty registry too.
The model is mocked at the ``LLMClient`` boundary; no network.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import grpc
import pytest

from agents.base import TaskInput, TaskInputConfig, TaskStatus
from agents.generated import task_pb2
from agents.llm_client import (
    LLMClient,
    LLMResponse,
    LLMToolResult,
    StopReason,
    ToolCall,
    Usage,
)
from agents.persona import create_persona_agent
from agents.persona_types import AgentEvent, EventType
from agents.server_servicers import AgentServiceServicer
from agents.sub_agents.delegation import DelegationRequest
from agents.sub_agents.spawner import SubAgentSpawner
from agents.task_agent import TaskAgent
from agents.tools.registry import ToolResult, clear_registry, tool
from agents.tools.tool_list import task_allows

from ._persona_test_helpers import _PERSONA_CONFIG

_NOTE_TOOLS = ["store_note", "recall_notes", "update_note", "delete_note"]


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    clear_registry()
    yield
    clear_registry()


def _register(ran: list[str], *names: str) -> None:
    """Register a no-argument tool under each of *names* that records its runs."""
    for name in names:
        def _make(tool_name: str) -> None:
            @tool(name=tool_name, description=f"{tool_name} stub")
            async def _stub() -> ToolResult:
                ran.append(tool_name)
                return ToolResult(success=True, data=f"{tool_name} ran")

        _make(name)


class _Scripted:
    """A real ``LLMClient`` over a mocked provider that records each round."""

    def __init__(self, *calls: str) -> None:
        self.rounds: list[list[LLMToolResult]] = []
        responses = [
            LLMResponse(
                text=None,
                tool_calls=[ToolCall(id=f"tc{i}", name=n, input={}) for i, n in enumerate(calls)],
                stop_reason=StopReason.TOOL_USE, usage=Usage(10, 10),
            ),
            LLMResponse(text="done", stop_reason=StopReason.END_TURN, usage=Usage(10, 10)),
        ]

        def _append(
            msgs: list[dict[str, Any]], _resp: LLMResponse, results: list[LLMToolResult],
        ) -> list[dict[str, Any]]:
            self.rounds.append(list(results))
            return [*msgs, {"role": "assistant", "content": "tool round"},
                    {"role": "user", "content": "tool results"}]

        self.provider = AsyncMock()
        self.provider.create_message = AsyncMock(side_effect=responses)
        self.provider.format_tool_definitions = MagicMock(side_effect=lambda defs: defs)
        self.provider.append_tool_round = MagicMock(side_effect=_append)
        self.client = LLMClient(self.provider)

    def offered(self) -> list[str]:
        """The tool names the model was offered on the turn's first call."""
        tools = self.provider.create_message.call_args_list[0].kwargs["tools"]
        return [d["name"] for d in tools]

    def answers(self) -> list[tuple[str, bool]]:
        [results] = self.rounds
        return [(r.content, r.is_error) for r in results]


def _task(*allowed: str) -> TaskInput:
    return TaskInput(
        task_id="t1", workflow_id="w1", payload="do it",
        config=TaskInputConfig(allowed_tools=list(allowed)),
    )


def _task_agent(script: _Scripted, *listed: str) -> TaskAgent:
    return TaskAgent(
        agent_id="code-writer",
        config={"model": "test-model", "role": "Writer", "tools": list(listed)},
        llm_client=script.client,
    )


async def _persona(script: _Scripted, *listed: str) -> Any:
    agent = create_persona_agent(
        agent_id="ember-owl", config={**_PERSONA_CONFIG, "tools": list(listed)},
        llm_client=script.client,
    )
    await agent.initialize_memory()
    return agent


class TestTaskAgent:
    async def test_a_tool_the_list_does_not_name_is_not_offered_and_does_not_run(
        self,
    ) -> None:
        ran: list[str] = []
        _register(ran, "alpha", "beta")
        script = _Scripted("beta", "alpha")

        output = await _task_agent(script, "alpha", "beta").handle(_task("alpha"))

        assert output.status == TaskStatus.COMPLETED
        assert script.offered() == ["alpha"]
        assert script.answers() == [("Unknown tool: beta", True), ("alpha ran", False)]
        assert ran == ["alpha"]

    async def test_an_empty_list_is_no_restriction(self) -> None:
        # The control for the test above: the same agent, the same calls.
        ran: list[str] = []
        _register(ran, "alpha", "beta")
        script = _Scripted("beta", "alpha")

        await _task_agent(script, "alpha", "beta").handle(_task())

        assert script.offered() == ["alpha", "beta"]
        assert script.answers() == [("beta ran", False), ("alpha ran", False)]
        assert ran == ["beta", "alpha"]

    async def test_the_list_cannot_add_a_tool_the_agent_does_not_list(self) -> None:
        ran: list[str] = []
        _register(ran, "alpha", "beta")
        script = _Scripted("beta", "alpha")

        await _task_agent(script, "alpha").handle(_task("alpha", "beta", "ghost"))

        assert script.offered() == ["alpha"]
        assert script.answers() == [("Unknown tool: beta", True), ("alpha ran", False)]
        assert ran == ["alpha"]

    async def test_a_list_that_names_no_tool_of_the_agents_leaves_it_none(self) -> None:
        ran: list[str] = []
        _register(ran, "alpha")
        script = _Scripted("alpha")

        await _task_agent(script, "alpha").handle(_task("ghost"))

        assert script.offered() == []
        assert script.answers() == [("Unknown tool: alpha", True)]
        assert ran == []

    async def test_the_refusal_is_logged_with_its_reason(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        _register([], "alpha", "beta")
        script = _Scripted("beta")

        with caplog.at_level("WARNING", logger="agents.tools.tool_list"):
            await _task_agent(script, "alpha", "beta").handle(_task("alpha"))

        [message] = [r.getMessage() for r in caplog.records
                     if r.name == "agents.tools.tool_list"]
        assert "'code-writer'" in message and "'beta'" in message
        assert "allowed_tools" in message


class TestPersona:
    async def test_the_list_narrows_the_personas_own_tools_too(self) -> None:
        ran: list[str] = []
        _register(ran, "alpha", "beta")
        script = _Scripted("beta", "store_note", "alpha", "recall_notes")
        agent = await _persona(script, "alpha", "beta")
        try:
            output = await agent.handle(_task("alpha", "recall_notes"))
        finally:
            await agent.close_memory()

        assert output.status == TaskStatus.COMPLETED
        assert script.offered() == ["alpha", "recall_notes"]
        contents = script.answers()
        assert contents[0] == ("Unknown tool: beta", True)
        assert contents[1] == ("Unknown tool: store_note", True)
        assert contents[2] == ("alpha ran", False)
        assert contents[3][1] is False
        assert ran == ["alpha"]

    async def test_an_empty_list_is_no_restriction(self) -> None:
        ran: list[str] = []
        _register(ran, "alpha", "beta")
        script = _Scripted("beta", "alpha")
        agent = await _persona(script, "alpha", "beta")
        try:
            await agent.handle(_task())
        finally:
            await agent.close_memory()

        assert script.offered() == ["alpha", "beta", *_NOTE_TOOLS]
        assert script.answers() == [("beta ran", False), ("alpha ran", False)]
        assert ran == ["beta", "alpha"]

    async def test_an_event_that_is_not_a_task_is_not_narrowed(self) -> None:
        # Only a task carries the list; a channel message has none to apply.
        ran: list[str] = []
        _register(ran, "alpha", "beta")
        script = _Scripted("beta")
        agent = await _persona(script, "alpha", "beta")
        try:
            await agent.on_event(AgentEvent(
                event_type=EventType.CHANNEL_MESSAGE,
                payload={"content": "hello", "respond_policy": "always"},
                channel_id="group:planning", sender_id="alice",
            ))
        finally:
            await agent.close_memory()

        assert script.offered() == ["alpha", "beta", *_NOTE_TOOLS]
        assert ran == ["beta"]


class TestFromTheWire:
    async def test_the_servicer_hands_the_list_to_the_tool_round(self) -> None:
        ran: list[str] = []
        _register(ran, "alpha", "beta")
        script = _Scripted("beta", "alpha")
        servicer = AgentServiceServicer(
            {"code-writer": _task_agent(script, "alpha", "beta")},
        )
        request = task_pb2.TaskRequest(
            task_id="t1", workflow_id="w1", agent_id="code-writer", payload="do it",
            config=task_pb2.TaskConfig(allowed_tools=["alpha"]),
        )

        response = await servicer.ExecuteTask(
            request, MagicMock(spec=grpc.aio.ServicerContext),
        )

        assert response.status == task_pb2.COMPLETED
        assert script.offered() == ["alpha"]
        assert ran == ["alpha"]

    async def test_an_unset_list_on_the_wire_is_no_restriction(self) -> None:
        # What the orchestrator sends today: it never sets the field.
        ran: list[str] = []
        _register(ran, "alpha", "beta")
        script = _Scripted("beta", "alpha")
        servicer = AgentServiceServicer(
            {"code-writer": _task_agent(script, "alpha", "beta")},
        )
        request = task_pb2.TaskRequest(
            task_id="t1", workflow_id="w1", agent_id="code-writer", payload="do it",
        )

        await servicer.ExecuteTask(request, MagicMock(spec=grpc.aio.ServicerContext))

        assert script.offered() == ["alpha", "beta"]
        assert ran == ["beta", "alpha"]

    async def test_a_delegations_list_binds_the_sub_agents_round(self) -> None:
        # Only tests reach the spawner today; its list arrives as the task's.
        ran: list[str] = []
        _register(ran, "alpha", "beta")
        script = _Scripted("beta", "alpha")
        child = _task_agent(script, "alpha", "beta")
        request = DelegationRequest(objective="do it", allowed_tools=frozenset({"alpha"}))

        spawned = await SubAgentSpawner("ember-owl").dispatch(child, request)

        assert spawned.raw_output.status == TaskStatus.COMPLETED
        assert script.offered() == ["alpha"]
        assert script.answers() == [("Unknown tool: beta", True), ("alpha ran", False)]
        assert ran == ["alpha"]


class TestTheRule:
    @pytest.mark.parametrize(
        ("allowed", "name", "allows"),
        [
            ((), "alpha", True),
            ([], "alpha", True),
            (frozenset(), "alpha", True),
            (None, "alpha", True),
            (["alpha"], "alpha", True),
            (["alpha"], "beta", False),
            (["alpha"], "alph", False),
            (["alpha", 7, None], "alpha", True),
            (["alpha", 7, None], "7", False),
            # A bare name is that one tool, never a set of letters to search.
            ("alpha", "alpha", True),
            ("alpha", "alph", False),
            ("alpha", "a", False),
            # A value that cannot be searched names no tool: it allows none.
            (7, "alpha", False),
            (True, "alpha", False),
        ],
    )
    def test_task_allows(self, allowed: Any, name: str, allows: bool) -> None:
        assert task_allows(allowed, name) is allows
