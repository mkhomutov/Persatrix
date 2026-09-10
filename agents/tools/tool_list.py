"""Which registered tools an agent's ``tools`` list gives it.

Each agent in ``config/agents.yaml`` lists the tools it may use. A task agent
is offered only the registered tools on that list
(:meth:`agents.base.BaseAgent._build_tool_definitions`) and runs only those
(:meth:`agents.base.BaseAgent._execute_tools`): a call to any other tool is
answered as if the tool did not exist (ISSUE-0151). Both ask this module, so
what an agent is offered and what it runs cannot drift apart again.

Persona agents keep their own copy of the rule in
``agents/persona_runtime/action_loop.py``, because they are also given their
memory tools and the recall tool from a list of their own.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .registry import ToolDefinition, get_tool, list_tools

__all__ = ["offered_tool", "offered_tools"]


def _listed_names(config: Mapping[str, Any]) -> frozenset[str]:
    """The tool names on *config*'s ``tools`` list.

    A ``tools:`` key with no value loads as ``None``, and a bare scalar is not
    a list; both name nothing. An entry that is not a string names no tool and
    is skipped, so one malformed entry cannot take the rest down with it.
    """
    listed = config.get("tools")
    if not isinstance(listed, (list, tuple)):
        return frozenset()
    # N-04: a set for O(1) membership checks as the tool registry grows.
    return frozenset(name for name in listed if isinstance(name, str))


def offered_tools(config: Mapping[str, Any]) -> list[ToolDefinition]:
    """The registered tools *config*'s ``tools`` list names, in registry order.

    S-12: an empty list offers nothing (e.g. the planner agent). A name no tool
    is registered under, such as an ``mcp:`` entry while the MCP bridge is not
    built, gives nothing (ISSUE-0147).
    """
    names = _listed_names(config)
    # F-04: early return avoids iterating the full registry when no tools
    # are configured for this agent.
    if not names:
        return []
    return [td for td in list_tools() if td.name in names]


def offered_tool(config: Mapping[str, Any], name: str) -> ToolDefinition | None:
    """The registered tool called *name*, if *config*'s ``tools`` list offers it.

    ``None`` both for a tool the list leaves out and for one that does not
    exist, so the caller answers the two alike and the model learns nothing
    about tools it was not given.
    """
    if name not in _listed_names(config):
        return None
    return get_tool(name)
