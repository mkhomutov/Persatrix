"""Which registered tools an agent's ``tools`` list gives it.

Each agent in ``config/agents.yaml`` lists the tools it may use. An agent is
offered only the registered tools on that list and runs only those: a call to
any other tool is answered as if the tool did not exist (ISSUE-0151). Task
agents (:class:`agents.base.BaseAgent`) and persona agents
(``agents/persona_runtime/tool_round.py``) both ask this module, both when
they offer tools and when they run them, so the two cannot drift apart again.
A persona is also given its memory tools and the recall tool, from a list of
its own; those sit on top of what this module returns.

A refused call is logged as a WARNING for the operator: the model learns only
``Unknown tool``, and a refused tool never runs, so no tool span or metric
would otherwise record that the model reached for a tool it was not given.

Both kinds of agent run a call only once :func:`admit_call` admits it: the
tool is one they offer, and its arguments pass the check in
:mod:`agents.tools.arg_check` (RFC 0009 Phase 3). A call whose arguments are
refused gets a structured ``tool_argument_validation_error`` instead, and is
recorded as a failed tool call (span and metrics) and in a
``tool.arg_invalid`` audit line. As the RFC 0009 Phases 3–4 plan's G-4 says,
that line is a structured log record on ``agents.tools.audit``: it reaches
the orchestrator's log stream but not its checksummed audit chain. What the
tool receives is the checked arguments, so RFC 0028 PR 10's pre-tool seat,
which will sit between the admission and the run, will see them too.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, NamedTuple

from ..llm_types import LLMToolResult, ToolCall
from ..observability.audit_record import emit_audit_record
from .arg_check import ArgumentRefusal, validate_arguments
from .registry import ToolDefinition, ToolResult, list_tools, record_refused_call

__all__ = ["AdmittedCall", "admit_call", "offered_tools", "refuse_call"]

logger = logging.getLogger(__name__)
_audit_logger = logging.getLogger("agents.tools.audit")


def _listed_names(agent_id: str, config: Mapping[str, Any]) -> frozenset[str]:
    """The tool names on *config*'s ``tools`` list.

    No ``tools`` key, or a ``tools:`` key with no value (which loads as
    ``None``), names nothing. A value that is not a list names nothing either,
    and an entry that is not a string is skipped, so one malformed entry cannot
    take the rest down with it; both are logged, because nothing checks the
    agent schema at start-up and the agent would otherwise lose its tools
    without a word.
    """
    listed = config.get("tools")
    if listed is None:
        return frozenset()
    if not isinstance(listed, (list, tuple)):
        logger.warning(
            "Agent %r: its tools setting is a %s, not a list, so it is offered no tools",
            agent_id, type(listed).__name__,
        )
        return frozenset()
    skipped = sum(1 for name in listed if not isinstance(name, str))
    if skipped:
        logger.warning(
            "Agent %r: skipped %d entries on its tools list that are not tool names",
            agent_id, skipped,
        )
    # N-04: a set for O(1) membership checks as the tool registry grows.
    return frozenset(name for name in listed if isinstance(name, str))


def offered_tools(agent_id: str, config: Mapping[str, Any]) -> list[ToolDefinition]:
    """The registered tools *config*'s ``tools`` list names, in registry order.

    S-12: an empty list offers nothing (e.g. the planner agent). A name no tool
    is registered under, such as an ``mcp:`` entry while the MCP bridge is not
    built, gives nothing (ISSUE-0147). *agent_id* only names the agent in a
    warning about a malformed list.
    """
    names = _listed_names(agent_id, config)
    # F-04: early return avoids iterating the full registry when no tools
    # are configured for this agent.
    if not names:
        return []
    return [td for td in list_tools() if td.name in names]


def refuse_call(agent_id: str, call: ToolCall) -> LLMToolResult:
    """Answer *call* as a call to a tool that does not exist, and log it.

    The model gets the same ``Unknown tool`` error whether the tool is missing
    from the agent's list, not registered, or has nothing to run, so it learns
    nothing about tools it was not given. The model chose the name, so the log
    line quotes it with escapes (CWE-117).
    """
    logger.warning(
        "Agent %r answered a call to tool %r with Unknown tool: "
        "it offers no runnable tool by that name",
        agent_id, call.name,
    )
    return LLMToolResult(
        tool_call_id=call.id,
        content=f"Unknown tool: {call.name}",
        is_error=True,
    )


class AdmittedCall(NamedTuple):
    """A call :func:`admit_call` let through: the tool to run and its arguments."""

    run: Callable[..., Awaitable[ToolResult]]
    arguments: dict[str, Any]


def admit_call(
    agent_id: str, tools: Mapping[str, ToolDefinition], call: ToolCall,
) -> AdmittedCall | LLMToolResult:
    """Admit *call* to run, or answer it with the result the model gets instead.

    *tools* maps each name the agent runs to the definition it runs under that
    name, so the check reads the tool that will actually run. A call to any
    other tool is answered by :func:`refuse_call`. A call whose arguments fail
    the check, or that the check itself fails on, does not run: running it on
    arguments nobody checked would hand the coming pre-tool seat a value the
    check never saw.
    """
    tool_def = tools.get(call.name)
    if tool_def is None or tool_def.func is None:
        return refuse_call(agent_id, call)
    try:
        checked = validate_arguments(tool_def.argument_spec, call.input)
    except Exception:
        logger.exception(
            "Agent %r could not check the arguments of a call to tool %r, "
            "so the call did not run", agent_id, call.name,
        )
        record_refused_call(agent_id=agent_id, tool_name=call.name, reason="argument_check_error")
        return LLMToolResult(tool_call_id=call.id, content="Internal tool error", is_error=True)
    if isinstance(checked, ArgumentRefusal):
        record_refused_call(
            agent_id=agent_id, tool_name=call.name, reason="tool_argument_validation_error",
        )
        emit_audit_record(
            _audit_logger, "tool.arg_invalid", agent_id=agent_id, tool_name=call.name,
            tool_call_id=call.id, **checked.audit_fields(),
        )
        return LLMToolResult(tool_call_id=call.id, content=checked.content(), is_error=True)
    return AdmittedCall(tool_def.func, checked)
