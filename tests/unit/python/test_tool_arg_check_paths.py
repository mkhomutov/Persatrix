"""Both places a tool runs check its arguments first (RFC 0009 Phase 3).

A tool runs in two places: a task agent's ``_execute_tools``
(``agents/task_tool_round.py``) and a persona's
(``agents/persona_runtime/tool_round.py``). Both now ask
:func:`agents.tools.tool_list.admit_call`, so a call the check refuses never
runs on either path, the model gets a structured
``tool_argument_validation_error``, the call counts as a failed tool call,
and one ``tool.arg_invalid`` audit line is written. The model is mocked at
the ``LLMClient`` boundary; no network.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Iterator
from typing import Any
from unittest.mock import patch

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from agents.llm_client import LLMToolResult, ToolCall
from agents.observability import logging as obs_logging
from agents.observability.redact import NoopRedactor
from agents.observability.spans import TOOL_EXECUTE_SPAN
from agents.persona import create_persona_agent
from agents.task_agent import TaskAgent
from agents.tools.registry import ToolDefinition, ToolResult, clear_registry, get_tool, tool
from agents.tools.tool_list import admit_call

from ._persona_test_helpers import _PERSONA_CONFIG, _make_client

Run = Callable[[list[ToolCall]], Awaitable[list[LLMToolResult]]]

_STANDARD_RECORD_KEYS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__)


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()
    obs_logging.set_redactor(NoopRedactor())


@pytest.fixture
def exporter() -> Iterator[InMemorySpanExporter]:
    """An in-memory span exporter on the active tracer provider."""
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        provider = TracerProvider()
        trace.set_tracer_provider(provider)
    exp = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exp)
    provider.add_span_processor(processor)
    yield exp
    processor.shutdown()


def _probe() -> ToolDefinition:
    td = get_tool("probe")
    assert td is not None
    return td


def _audit_record(caplog: pytest.LogCaptureFixture) -> logging.LogRecord:
    [record] = [r for r in caplog.records if r.name == "agents.tools.audit"]
    return record


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

        record = _audit_record(caplog)
        assert record.levelno == logging.INFO
        assert record.getMessage() == "tool.arg_invalid"
        fields = record.__dict__
        assert fields["audit"] is True
        assert (fields["tool_name"], fields["tool_call_id"]) == ("probe", "c7")
        assert fields["agent_id"] in ("code-reviewer", "ember-owl")
        assert fields["input_type"] == "object"
        assert fields["missing_arguments"] == ["command"]
        assert fields["unknown_argument_count"] == 1
        assert fields["invalid_arguments"] == []
        extra = {k: v for k, v in fields.items() if k not in _STANDARD_RECORD_KEYS}
        assert "SECRET-123" not in json.dumps(extra, default=str)
        assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []

    async def test_the_redactor_runs_over_the_audit_line(
        self, run: Run, caplog: pytest.LogCaptureFixture,
    ) -> None:
        class _Mask:
            def redact(self, record: dict[str, Any]) -> dict[str, Any]:
                return {**record, "tool_call_id": "[REDACTED]"}

        _register([])
        obs_logging.set_redactor(_Mask())

        with caplog.at_level(logging.INFO, logger="agents.tools.audit"):
            await run([ToolCall(id="c1", name="probe", input={"cmd": 1})])

        assert _audit_record(caplog).__dict__["tool_call_id"] == "[REDACTED]"

    async def test_an_unknown_name_stays_out_of_the_audit_line(
        self, run: Run, caplog: pytest.LogCaptureFixture,
    ) -> None:
        # The model chooses a name as freely as a value, so a name can carry
        # what a value would; the audit line counts the names and the model,
        # which sent them, is the one told which they were.
        _register([])

        with caplog.at_level(logging.INFO, logger="agents.tools.audit"):
            [result] = await run([ToolCall(
                id="c1", name="probe", input={"command": "ls", "sk-live-abc123": 1},
            )])

        fields = _audit_record(caplog).__dict__
        extra = {k: v for k, v in fields.items() if k not in _STANDARD_RECORD_KEYS}
        assert "sk-live" not in json.dumps(extra, default=str)
        assert fields["unknown_argument_count"] == 1
        assert _body(result)["unknown"] == ["sk-live-abc123"]

    async def test_a_redactor_fallback_that_logs_an_audit_line_does_not_recurse(
        self, run: Run, caplog: pytest.LogCaptureFixture,
    ) -> None:
        # A handler that audits its own records re-enters the audit helper
        # from the fallback WARNING; the redactor must not run again there.
        calls: list[int] = []

        class _Boom:
            def redact(self, record: dict[str, Any]) -> dict[str, Any]:
                calls.append(1)
                raise RuntimeError("boom")

        class _Reenter(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                if record.levelno == logging.WARNING:
                    admit_call("ember-owl", {"probe": _probe()}, ToolCall(
                        id="c2", name="probe", input={"cmd": "ls"},
                    ))

        _register([])
        obs_logging.set_redactor(_Boom())
        handler = _Reenter()
        audit_logger = logging.getLogger("agents.tools.audit")
        audit_logger.addHandler(handler)
        try:
            with caplog.at_level(logging.INFO, logger="agents.tools.audit"):
                await run([ToolCall(id="c1", name="probe", input={"cmd": "ls"})])
        finally:
            audit_logger.removeHandler(handler)

        assert calls == [1]
        infos = [r for r in caplog.records
                 if r.name == "agents.tools.audit" and r.levelno == logging.INFO]
        assert len(infos) == 2

    async def test_a_refusal_records_a_failed_tool_span_and_metric(
        self, run: Run, exporter: InMemorySpanExporter,
    ) -> None:
        # A misspelt argument used to fail inside the tool's wrapper, which
        # recorded the failure; the tool failure rate a dashboard reads must
        # still count the call now that it is refused before the tool runs.
        seen: list[dict[str, Any]] = []
        _register(seen)

        with patch("agents.tools.registry._record_tool_metrics") as metrics:
            await run([ToolCall(id="c1", name="probe", input={"cmd": "ls"})])

        assert seen == []
        [span] = [s for s in exporter.get_finished_spans() if s.name == TOOL_EXECUTE_SPAN]
        assert span.attributes is not None
        assert (span.attributes["tool.name"], span.attributes["tool.success"]) == ("probe", False)
        assert span.status.status_code is StatusCode.ERROR
        metrics.assert_called_once()
        assert metrics.call_args.kwargs["tool_name"] == "probe"
        assert metrics.call_args.kwargs["success"] is False

    async def test_a_redactor_that_raises_still_refuses_the_call(
        self, run: Run, caplog: pytest.LogCaptureFixture,
    ) -> None:
        class _Boom:
            def redact(self, record: dict[str, Any]) -> dict[str, Any]:
                raise RuntimeError("boom")

        seen: list[dict[str, Any]] = []
        _register(seen)
        obs_logging.set_redactor(_Boom())

        with caplog.at_level(logging.INFO, logger="agents.tools.audit"):
            [result] = await run([ToolCall(id="c1", name="probe", input={"cmd": "ls"})])

        assert _body(result)["unknown"] == ["cmd"]
        assert seen == []
        levels = [r.levelno for r in caplog.records if r.name == "agents.tools.audit"]
        assert levels == [logging.WARNING, logging.INFO]

    async def test_a_check_that_fails_refuses_the_call(
        self, run: Run, caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Running the tool on unchecked arguments would hand RFC 0028's coming
        # pre-tool seat a value the check never saw, so the call is refused,
        # and the operator's log line is the one record of why.
        seen: list[dict[str, Any]] = []
        _register(seen)

        with (
            patch("agents.tools.tool_list.validate_arguments", side_effect=RuntimeError),
            patch("agents.tools.registry._record_tool_metrics") as metrics,
        ):
            [result] = await run([ToolCall(id="c1", name="probe", input={"command": "ls"})])

        assert (result.content, result.is_error) == ("Internal tool error", True)
        assert seen == []
        assert metrics.call_args.kwargs["success"] is False
        [record] = [r for r in caplog.records if r.name == "agents.tools.tool_list"]
        assert record.levelno == logging.ERROR and record.exc_info is not None
        assert "'probe'" in record.getMessage()


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
