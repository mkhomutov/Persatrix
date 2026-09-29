"""The tools a persona turn offers the model, and the calls it runs.

A persona's turn is a loop of model calls (:mod:`.action_loop`): a reply
that asks for tools runs them and goes round again with their results. This
mixin holds the two halves of that round trip: the tool definitions the
turn offers, and running the calls the model makes. Moved out of
``action_loop.py`` unchanged when that file passed its size warning.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..llm_client import LLMToolResult, ToolCall
from ..security import maybe_wrap_tool_content
from ..tools.registry import ToolDefinition
from ..tools.tool_list import offered_tools, refuse_call

logger = logging.getLogger(__name__)

__all__ = ["_ToolRoundMixin"]


class _ToolRoundMixin:
    """The tools a persona turn offers, and running the calls the model makes."""

    # Attribute declarations for type checkers — set by __init__ or base classes.
    agent_id: str
    config: dict[str, Any]
    _memory_tools: list[ToolDefinition]

    def _build_tool_definitions(self) -> list[dict[str, Any]]:
        """Build tool definitions including memory tools.

        Uses a dict keyed by tool name so memory tools take precedence
        over registry tools with the same name (F-5a-2: defense-in-depth,
        memory tools should shadow any same-named registry tools).
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

        return list(defs_by_name.values())

    async def _execute_tools(self, tool_calls: list[ToolCall]) -> list[LLMToolResult]:
        """Execute tool calls, checking memory tools first then registry.

        Registry lookups are restricted to tools in ``config["tools"]``
        (F-5a-2: defense-in-depth against LLM hallucinating tool names
        that exist in the global registry but weren't offered to this agent).
        """
        # Offered registry tools, then memory tools (always allowed) shadowing
        # any of the same name, exactly as _build_tool_definitions() offers them.
        tool_map = {td.name: td for td in offered_tools(self.agent_id, self.config)}
        tool_map.update({td.name: td for td in self._memory_tools})
        results: list[LLMToolResult] = []

        for call in tool_calls:
            tool_def = tool_map.get(call.name)
            if tool_def is None or tool_def.func is None:
                results.append(refuse_call(self.agent_id, call))
                continue

            try:
                result = await tool_def.func(**call.input)
                if result.success:
                    content = (
                        json.dumps(result.data)
                        if isinstance(result.data, (dict, list))
                        else str(result.data)
                    )
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
                        output = (
                            json.dumps(result.data)
                            if isinstance(result.data, (dict, list))
                            else str(result.data)
                        )
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
