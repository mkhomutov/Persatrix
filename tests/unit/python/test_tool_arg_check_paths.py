"""Both places a tool runs check its arguments first (RFC 0009 PR 3).

A tool runs in two places: a task agent's ``_execute_tools``
(``agents/base.py``) and a persona's (``agents/persona_runtime/tool_round.py``).
Both now ask :func:`agents.tools.tool_list.admit_call`, so a call the check
refuses never runs on either path, the model gets a structured
``tool_argument_validation_error``, and one ``tool.arg_invalid`` audit line
is written. The model is mocked at the ``LLMClient`` boundary; no network.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import patch

import pytest

from agents.llm_client import LLMToolResult, ToolCall
from agents.persona import create_persona_agent
from agents.task_agent import TaskAgent
from agents.tools.registry import ToolDefinition, ToolResult, clear_registry, get_tool, tool

from ._persona_test_helpers import _PERSONA_CONFIG, _make_client

Run = Callable[[list[ToolCall]], Awaitable[list[LLMToolResult]]]


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


def _register(seen: list[dict[str, Any]]) -> None:
    """A tool shaped like ``shell_exec`` that records what it receives."""

    @tool(name="probe", description="probe")
    async def probe(command: str, timeout: int = 30) -> ToolResult:
        seen.append({"command": command, "timeout": timeout})
        return ToolResult(success=True, data="ran")


@pytest.fixture(params=["task", "persona"])
async def run(request: pytest.FixtureRequest) -> Any:
    """Run tool calls on the task-agent path or the persona path."""
    if request.param == "task":
        agent: Any = TaskAgent(agent_id="code-reviewer", config={"tools": ["probe"]})
        yield agent._execute_tools
        return
    agent = create_persona_agent(
        agent_id="ember-owl", config={**_PERSONA_CONFIG, "tools": ["probe"]},
        llm_client=_make_client(),
    )
    await agent.initialize_memory()
    yield agent._execute_tools
    await agent.close_memory()


def _body(result: LLMToolResult) -> dict[str, Any]:
    assert result.is_error
    body: dict[str, Any] = json.loads(result.content)
    return body


class TestRefusedBeforeTheToolRuns:
    async def test_a_missing_argument(self, run: Run) -> None:
        seen: list[dict[str, Any]] = []
        _register(seen)

        [result] = await run([ToolCall(id="c1", name="probe", input={"timeout": 5})])

        assert _body(result)["missing"] == ["command"]
        assert seen == []

    async def test_an_unknown_argument(self, run: Run) -> None:
        seen: list[dict[str, Any]] = []
        _register(seen)

        [result] = await run([
            ToolCall(id="c1", name="probe", input={"command": "ls", "cwd": "/"}),
        ])

        assert _body(result)["unknown"] == ["cwd"]
        assert seen == []

    async def test_arguments_that_are_not_an_object(self, run: Run) -> None:
        seen: list[dict[str, Any]] = []
        _register(seen)

        [result] = await run([ToolCall(id="c1", name="probe", input=["ls"])])  # type: ignore[arg-type]

        assert _body(result)["tool_argument_validation_error"].startswith(
            "arguments must be a JSON object, not array",
        )
        assert seen == []


class TestCheckedArgumentsReachTheTool:
    async def test_integer_text_reaches_the_tool_as_an_int(self, run: Run) -> None:
        seen: list[dict[str, Any]] = []
        _register(seen)
        arguments = {"command": "ls", "timeout": "30"}

        [result] = await run([ToolCall(id="c1", name="probe", input=arguments)])

        assert (result.content, result.is_error) == ("ran", False)
        assert seen == [{"command": "ls", "timeout": 30}]
        assert type(seen[0]["timeout"]) is int
        assert arguments == {"command": "ls", "timeout": "30"}


class TestOrderAndAudit:
    async def test_an_unknown_tool_is_refused_before_any_check(
        self, run: Run, caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.INFO, logger="agents.tools.audit"):
            [result] = await run([ToolCall(id="c1", name="ghost", input=[1])])  # type: ignore[arg-type]

        assert result.content == "Unknown tool: ghost"
        assert [r for r in caplog.records if r.name == "agents.tools.audit"] == []

    async def test_a_refusal_writes_one_audit_line(
        self, run: Run, caplog: pytest.LogCaptureFixture,
    ) -> None:
        _register([])

        with caplog.at_level(logging.INFO):
            await run([ToolCall(id="c7", name="probe", input={"cmd": "SECRET-123"})])

        [record] = [r for r in caplog.records if r.name == "agents.tools.audit"]
        assert record.levelno == logging.INFO
        assert record.getMessage() == "tool.arg_invalid"
        fields = record.__dict__
        assert fields["audit"] is True
        assert (fields["tool_name"], fields["tool_call_id"]) == ("probe", "c7")
        assert fields["agent_id"] in ("code-reviewer", "ember-owl")
        assert fields["input_type"] == "object"
        assert fields["missing_arguments"] == ["command"]
        assert fields["unknown_arguments"] == ["cmd"]
        assert fields["unknown_argument_count"] == 1
        assert fields["invalid_arguments"] == []
        assert "SECRET-123" not in json.dumps(
            {k: v for k, v in fields.items() if k.endswith(("arguments", "_type", "count"))},
        )
        assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []

    async def test_a_check_that_fails_refuses_the_call(self, run: Run) -> None:
        # Running the tool on unchecked arguments would hand RFC 0028's
        # pre-tool seat a value the check never saw, so the call is refused.
        seen: list[dict[str, Any]] = []
        _register(seen)

        with patch("agents.tools.tool_list.validate_arguments", side_effect=RuntimeError):
            [result] = await run([ToolCall(id="c1", name="probe", input={"command": "ls"})])

        assert (result.content, result.is_error) == ("Internal tool error", True)
        assert seen == []


async def test_a_persona_checks_the_memory_tool_that_shadows_a_registry_tool() -> None:
    # A persona runs its own memory tool in place of a registry tool of the
    # same name, so the check must read the definition the persona runs, not
    # whatever the global registry holds under that name.
    agent = create_persona_agent(
        agent_id="ember-owl", config={**_PERSONA_CONFIG, "tools": ["store_note"]},
        llm_client=_make_client(),
    )
    await agent.initialize_memory()

    @tool(name="store_note")
    async def registry_note(other: str) -> ToolResult:
        return ToolResult(success=True, data="registry ran")

    registry_def = get_tool("store_note")
    memory_def = next(td for td in agent._memory_tools if td.name == "store_note")
    assert isinstance(memory_def, ToolDefinition) and memory_def is not registry_def

    [result] = await agent._execute_tools([
        ToolCall(id="c1", name="store_note", input={"topic": "t", "content": "c"}),
    ])

    assert result.is_error is False, result.content
    assert result.content != "registry ran"
    await agent.close_memory()
