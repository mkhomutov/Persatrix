"""Which registered tools an agent's ``tools`` list gives it.

Each agent in ``config/agents.yaml`` lists the tools it may use. An agent is
offered only the registered tools on that list and runs only those: a call to
any other tool is answered as if the tool did not exist (ISSUE-0151). Task
agents (:class:`agents.base.BaseAgent`) and persona agents
(``agents/persona_runtime/action_loop.py``) both ask this module, both when
they offer tools and when they run them, so the two cannot drift apart again.
A persona is also given its memory tools and the recall tool, from a list of
its own; those sit on top of what this module returns.

A refused call is logged as a WARNING for the operator: the model learns only
``Unknown tool``, and a refused tool never runs, so no tool span or metric
would otherwise record that the model reached for a tool it was not given.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from ..llm_types import LLMToolResult, ToolCall
from .registry import ToolDefinition, list_tools

__all__ = ["offered_tools", "refuse_call"]

logger = logging.getLogger(__name__)


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
