"""A tool runs only for an agent whose config grants what the tool declares (ISSUE-0214).

``@tool(permissions=[...])`` used to be stored and never read: a tool that
declared ``filesystem:read`` ran for an agent with no ``filesystem`` block.
The one admission function both agent kinds use,
:func:`agents.tools.tool_list.admit_call`, now refuses such a call before
the tool runs. The model gets the answer the built-in tools already gave
from their own bodies (``Permission denied: filesystem:read``), the operator
gets a WARNING, and the call counts as a failed tool call.

Each "is refused" test has a positive control: the same tool runs once the
agent's config grants the permission. The model is mocked at the
``LLMClient`` boundary; no network.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import replace
from typing import Any
from unittest.mock import patch

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from agents.llm_client import LLMToolResult, ToolCall
from agents.observability.spans import TOOL_EXECUTE_SPAN
from agents.persona import create_persona_agent
from agents.task_agent import TaskAgent
from agents.tools.registry import ToolDefinition, ToolResult, clear_registry, get_tool, tool
from agents.tools.tool_list import AdmittedCall, admit_call

from ._persona_test_helpers import _PERSONA_CONFIG, _make_client

Run = Callable[[list[ToolCall]], Awaitable[list[LLMToolResult]]]
MakeRun = Callable[[dict[str, Any] | None], Awaitable[Run]]

_READ = {"filesystem": {"read": ["/workspace/**"]}}


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    clear_registry()
    yield
    clear_registry()


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


def _register(ran: list[str], *declared: str, name: str = "probe") -> None:
    """Register a tool that declares *declared* and records each run."""

    @tool(name=name, description="probe", permissions=list(declared))
    async def _probe(path: str = "/workspace/a.txt") -> ToolResult:
        ran.append(name)
        return ToolResult(success=True, data=f"{name} ran")


@pytest.fixture(params=["task", "persona"])
async def make_run(request: pytest.FixtureRequest) -> Any:
    """Build one agent of each kind from a ``permissions`` block; give its tool round.

    Both kinds list the tools ``probe`` and ``second``. ``None`` leaves the
    ``permissions`` key out of the agent's config altogether.
    """
    opened: list[Any] = []

    async def _make(permissions: dict[str, Any] | None) -> Run:
        extra: dict[str, Any] = {"tools": ["probe", "second"]}
        if permissions is not None:
            extra["permissions"] = permissions
        if request.param == "task":
            task_agent = TaskAgent(agent_id="code-reviewer", config=extra)
            return task_agent._execute_tools
        config = {k: v for k, v in _PERSONA_CONFIG.items() if k != "permissions"}
        persona = create_persona_agent(
            agent_id="ember-owl", config={**config, **extra}, llm_client=_make_client(),
        )
        await persona.initialize_memory()
        opened.append(persona)
        return persona._execute_tools

    yield _make
    for persona in opened:
        await persona.close_memory()


def _call(name: str = "probe", **arguments: Any) -> ToolCall:
    return ToolCall(id="c1", name=name, input=arguments)


class TestRefusedBeforeTheToolRuns:
    @pytest.mark.parametrize(
        "permissions",
        [
            None,
            {},
            {"memory": {"read": True, "write": True}},
            {"filesystem": {}},
            {"filesystem": {"read": []}},
            {"filesystem": {"write": ["/workspace/**"], "deny": ["**/.env*"]}},
        ],
        ids=["no-block", "empty-block", "other-category", "empty-category",
             "empty-list", "write-and-deny-only"],
    )
    async def test_a_tool_whose_permission_the_agent_lacks_does_not_run(
        self, make_run: MakeRun, permissions: dict[str, Any] | None,
    ) -> None:
        ran: list[str] = []
        _register(ran, "filesystem:read")

        [refused] = await (await make_run(permissions))([_call()])

        assert (refused.content, refused.is_error) == (
            "Permission denied: filesystem:read", True,
        )
        assert ran == []
        # Positive control: the same call runs once the config grants it.
        [granted] = await (await make_run(_READ))([_call()])
        assert (granted.content, granted.is_error) == ("probe ran", False)
        assert ran == ["probe"]

    async def test_every_declared_permission_must_be_granted(
        self, make_run: MakeRun,
    ) -> None:
        ran: list[str] = []
        _register(ran, "filesystem:read", "memory:write")

        [refused] = await (await make_run(_READ))([_call()])

        # The first missing one is named; the granted one is not enough.
        assert refused.content == "Permission denied: memory:write"
        assert ran == []
        both = {**_READ, "memory": {"write": True}}
        [granted] = await (await make_run(both))([_call()])
        assert (granted.content, ran) == ("probe ran", ["probe"])

    async def test_a_tool_that_declares_nothing_runs_as_before(
        self, make_run: MakeRun,
    ) -> None:
        # Such a tool is held only by the agent's ``tools`` list (ISSUE-0151).
        ran: list[str] = []
        _register(ran)

        [result] = await (await make_run(None))([_call()])

        assert (result.content, result.is_error) == ("probe ran", False)
        assert ran == ["probe"]

    async def test_one_refused_call_does_not_stop_the_next(
        self, make_run: MakeRun,
    ) -> None:
        ran: list[str] = []
        _register(ran, "memory:read")
        _register(ran, "filesystem:read", name="second")

        refused, granted = await (await make_run(_READ))([_call(), _call("second")])

        assert refused.content == "Permission denied: memory:read"
        assert (granted.content, ran) == ("second ran", ["second"])


class TestOrder:
    async def test_the_permission_is_checked_before_the_arguments(
        self, make_run: MakeRun, caplog: pytest.LogCaptureFixture,
    ) -> None:
        # A tool the agent may not use has none of its arguments looked at:
        # no ``tool.arg_invalid`` audit line, and no argument named back.
        ran: list[str] = []
        _register(ran, "filesystem:read")

        with caplog.at_level(logging.INFO, logger="agents.tools.audit"):
            [refused] = await (await make_run({}))([_call(nonsense=1)])

        assert refused.content == "Permission denied: filesystem:read"
        assert [r for r in caplog.records if r.name == "agents.tools.audit"] == []
        # Control: with the permission, the same call reaches the argument check.
        with caplog.at_level(logging.INFO, logger="agents.tools.audit"):
            [checked] = await (await make_run(_READ))([_call(nonsense=1)])
        assert "tool_argument_validation_error" in checked.content
        assert ran == []

    async def test_a_tool_off_the_agents_list_stays_unknown(
        self, make_run: MakeRun,
    ) -> None:
        # The model learns nothing about a tool it was not given, not even
        # which permission it would need.
        ran: list[str] = []
        _register(ran, "filesystem:read", name="unlisted")

        [result] = await (await make_run({}))([_call("unlisted")])

        assert result.content == "Unknown tool: unlisted"
        assert ran == []


class TestOperatorSignals:
    async def test_a_refusal_is_logged_as_a_warning(
        self, make_run: MakeRun, caplog: pytest.LogCaptureFixture,
    ) -> None:
        _register([], "filesystem:read")
        run = await make_run({})

        with caplog.at_level(logging.DEBUG, logger="agents.tools.tool_list"):
            await run([_call()])

        [record] = [r for r in caplog.records if r.name == "agents.tools.tool_list"]
        assert record.levelno == logging.WARNING
        message = record.getMessage()
        assert "'probe'" in message and "'filesystem:read'" in message
        assert "'code-reviewer'" in message or "'ember-owl'" in message

    async def test_a_granted_call_logs_no_warning(
        self, make_run: MakeRun, caplog: pytest.LogCaptureFixture,
    ) -> None:
        _register([], "filesystem:read")
        run = await make_run(_READ)

        with caplog.at_level(logging.DEBUG, logger="agents.tools.tool_list"):
            await run([_call()])

        assert [r for r in caplog.records if r.name == "agents.tools.tool_list"] == []

    async def test_a_refusal_counts_as_one_failed_tool_call(
        self, make_run: MakeRun, exporter: InMemorySpanExporter,
    ) -> None:
        # A built-in tool's own refusal went through the ``@tool`` wrapper,
        # which recorded a failed call; the failure rate a dashboard reads
        # must still count it now that the call stops before the wrapper.
        _register([], "filesystem:read")
        run = await make_run({})

        with patch("agents.tools.registry._record_tool_metrics") as metrics:
            await run([_call()])

        [span] = [s for s in exporter.get_finished_spans() if s.name == TOOL_EXECUTE_SPAN]
        assert span.attributes is not None
        assert (span.attributes["tool.name"], span.attributes["tool.success"]) == ("probe", False)
        assert span.status.status_code is StatusCode.ERROR
        assert span.status.description == "permission_denied"
        metrics.assert_called_once()
        assert metrics.call_args.kwargs["tool_name"] == "probe"
        assert metrics.call_args.kwargs["success"] is False


class TestFailsClosed:
    @pytest.mark.parametrize(
        "permissions",
        ["all", ["filesystem:read"], {"filesystem": True}, {"filesystem": ["read"]}],
        ids=["text", "list", "category-is-true", "category-is-a-list"],
    )
    async def test_a_permissions_block_of_the_wrong_shape_grants_nothing(
        self, make_run: MakeRun, permissions: Any,
    ) -> None:
        # Nothing checks the agent schema at start-up, so a YAML slip reaches
        # the check. It must refuse the call, never raise out of the round.
        ran: list[str] = []
        _register(ran, "filesystem:read")

        [result] = await (await make_run(permissions))([_call()])

        assert (result.content, result.is_error) == (
            "Permission denied: filesystem:read", True,
        )
        assert ran == []

    async def test_a_check_that_raises_refuses_the_call(
        self, make_run: MakeRun, caplog: pytest.LogCaptureFixture,
    ) -> None:
        ran: list[str] = []
        _register(ran, "filesystem:read")
        run = await make_run(_READ)

        with (
            patch("agents.tools.tool_list.PermissionGate", side_effect=RuntimeError("boom")),
            patch("agents.tools.registry._record_tool_metrics") as metrics,
        ):
            [result] = await run([_call()])

        assert (result.content, result.is_error) == ("Internal tool error", True)
        assert ran == []
        assert metrics.call_args.kwargs["success"] is False
        [record] = [r for r in caplog.records if r.name == "agents.tools.tool_list"]
        assert record.levelno == logging.ERROR and record.exc_info is not None
        assert "'probe'" in record.getMessage()

    @pytest.mark.parametrize(
        "declared",
        ["db:read", "filesystem:deny", "network:deny", "network:allow",
         "shell:allowed_commands", "shell:max_execution_seconds", "filesystem", ""],
    )
    async def test_a_name_no_config_can_grant_is_refused_whatever_the_config_holds(
        self, make_run: MakeRun, declared: str,
    ) -> None:
        # ``@tool`` refuses to register such a name; a definition built by
        # hand can still carry one. Every key named here is set in the
        # config, so a check that only looked the key up would grant it.
        ran: list[str] = []
        _register(ran)
        registered = get_tool("probe")
        assert registered is not None
        registered.permissions = [declared]
        permissions = {
            "db": {"read": True},
            "filesystem": {"read": ["/w/**"], "write": ["/w/**"], "deny": ["**/.env*"]},
            "network": {"allow": ["example.com"], "deny": ["*"]},
            "shell": {"allowed_commands": ["ls"], "max_execution_seconds": 30},
        }

        [result] = await (await make_run(permissions))([_call()])

        assert (result.content, result.is_error) == (f"Permission denied: {declared}", True)
        assert ran == []
        # Control: with nothing declared the same agent runs the tool.
        registered.permissions = []
        [control] = await (await make_run(permissions))([_call()])
        assert (control.content, ran) == ("probe ran", ["probe"])


class TestOnePlaceForBothAgentKinds:
    def test_no_caller_can_leave_the_permissions_or_the_task_list_out(self) -> None:
        # Both are keyword-only and have no default: a third caller of
        # ``admit_call`` cannot run a tool without saying whose config and
        # which task list the call is held to.
        parameters = inspect.signature(admit_call).parameters

        for name in ("permissions", "allowed_tools"):
            assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
            assert parameters[name].default is inspect.Parameter.empty

    def test_admit_call_reads_the_definition_it_is_handed(self) -> None:
        # The check reads the tool that will run, so a persona's own memory
        # tool is held to what *it* declares, not to a registry tool's.
        # The registry's tool of that name declares nothing; the one handed
        # in declares a permission.
        _register([])
        registered = get_tool("probe")
        assert isinstance(registered, ToolDefinition) and registered.permissions == []
        handed = replace(registered, permissions=["channels:recall"])

        refused = admit_call(
            "t", {"probe": handed}, _call(), permissions={}, allowed_tools=(),
        )
        admitted = admit_call(
            "t", {"probe": handed}, _call(),
            permissions={"channels": {"recall": True}}, allowed_tools=(),
        )

        assert isinstance(refused, LLMToolResult)
        assert refused.content == "Permission denied: channels:recall"
        assert isinstance(admitted, AdmittedCall)
