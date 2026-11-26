"""The tools a persona turn offers the model, and the calls it runs.

A persona's turn is a loop of model calls (:mod:`.action_loop`): a reply
that asks for tools runs them and goes round again with their results. This
mixin holds the two halves of that round trip: the tool definitions the
turn offers, and running the calls the model makes. Moved out of
``action_loop.py`` unchanged when that file passed its size warning.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from typing import Any

from ..llm_client import LLMToolResult, ToolCall
from ..persona_types import AgentEvent, EventType
from ..security import maybe_wrap_tool_content
from ..task_types import TaskInput
from ..tools.registry import ToolDefinition, shown_output
from ..tools.tool_list import admit_call, offered_tools, task_allows

logger = logging.getLogger(__name__)

__all__ = ["_ToolRoundMixin", "allowed_tools_of"]


def allowed_tools_of(event: AgentEvent) -> Collection[str]:
    """The ``allowed_tools`` list of the task *event* carries, or an empty one.

    Only a workflow task carries the list (``TaskConfig.allowed_tools``); a
    persona's turn on any other event is held to no task list.
    """
    task = event.payload.get("task") if event.event_type is EventType.TASK_ASSIGNED else None
    return task.config.allowed_tools if isinstance(task, TaskInput) else ()


class _ToolRoundMixin:
    """The tools a persona turn offers, and running the calls the model makes."""

    # Attribute declarations for type checkers — set by __init__ or base classes.
    agent_id: str
    config: dict[str, Any]
    _memory_tools: list[ToolDefinition]

    def _build_tool_definitions(
        self, allowed_tools: Collection[str] = (),
    ) -> list[dict[str, Any]]:
        """Build tool definitions including memory tools.

        Uses a dict keyed by tool name so memory tools take precedence
        over registry tools with the same name (F-5a-2: defense-in-depth,
        memory tools should shadow any same-named registry tools).
        *allowed_tools* is a task's own list (ISSUE-0214): a tool it does
        not allow is left out, a memory tool included. Empty, the default,
        leaves none out.
        """
        # Start with agent-configured tools from the global registry, by the
        # rule task agents share (ISSUE-0151: agents.tools.tool_list).
        defs_by_name: dict[str, dict[str, Any]] = {}

        for td in offered_tools(self.agent_id, self.config):
            defs_by_name[td.name] = {
                "name": td.name,
                "description": td.description,
                "parameters": td.parameters,
            }

        # Memory tools override registry tools with the same name,
        # consistent with _execute_tools() which checks memory tools first.
        for td in self._memory_tools:
            defs_by_name[td.name] = {
                "name": td.name,
                "description": td.description,
                "parameters": td.parameters,
            }

        return [d for d in defs_by_name.values() if task_allows(allowed_tools, d["name"])]

    async def _execute_tools(
        self, tool_calls: list[ToolCall], allowed_tools: Collection[str] = (),
    ) -> list[LLMToolResult]:
        """Execute tool calls, checking memory tools first then registry.

        Registry lookups are restricted to tools in ``config["tools"]``
        (F-5a-2: defense-in-depth against LLM hallucinating tool names
        that exist in the global registry but weren't offered to this agent),
        and a call runs only on arguments that pass the check (RFC 0009 Phase 3).
        ISSUE-0214: ``admit_call`` also refuses a tool a task's *allowed_tools*
        does not allow, and one that declares a permission this agent's
        config does not grant; a memory tool is held to both as well.
        """
        # Offered registry tools, then memory tools (always allowed) shadowing
        # any of the same name, exactly as _build_tool_definitions() offers them.
        tool_map = {td.name: td for td in offered_tools(self.agent_id, self.config)}
        tool_map.update({td.name: td for td in self._memory_tools})
        results: list[LLMToolResult] = []

        for call in tool_calls:
            admitted = admit_call(
                self.agent_id, tool_map, call,
                permissions=self.config.get("permissions"), allowed_tools=allowed_tools,
            )
            if isinstance(admitted, LLMToolResult):
                results.append(admitted)
                continue

            try:
                result = await admitted.run(**admitted.arguments)
                if result.success:
                    content = shown_output(result)
                    # RFC 0009 PR 3: external-data tools wrapped here.
                    content = maybe_wrap_tool_content(call.name, content)
                else:
                    error_msg = result.error or "Tool failed"
                    if result.error_type:
                        content = f"Tool error ({result.error_type}): {error_msg}"
                    else:
                        content = error_msg
                    # A failure can still carry output (shell_exec's stdout
                    # and stderr on a non-zero exit); the model needs it.
                    if result.data:
                        output = shown_output(result)
                        content += "\n" + maybe_wrap_tool_content(call.name, output)
                results.append(LLMToolResult(
                    tool_call_id=call.id,
                    content=content,
                    is_error=not result.success,
                ))
            except Exception as exc:
                logger.warning("Unexpected error in tool %s: %s", call.name, exc)
                results.append(LLMToolResult(
                    tool_call_id=call.id,
                    content="Internal tool error",
                    is_error=True,
                ))

        return results
