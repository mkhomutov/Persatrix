"""The tools a task agent's turn offers the model, and the calls it runs.

A task agent's turn is a loop of model calls (``BaseAgent._run_llm_loop``):
a reply that asks for tools runs them and goes round again with their
results. This mixin holds the two halves of that round trip, as
:mod:`agents.persona_runtime.tool_round` does for personas, whose mixin
comes first in a persona's MRO and so replaces this one. Moved out of
``agents/base.py`` unchanged when that file passed its size warning.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .llm_client import LLMToolResult, ToolCall
from .security import maybe_wrap_tool_content
from .tools.tool_list import admit_call, offered_tools

logger = logging.getLogger(__name__)

__all__ = ["_TaskToolRoundMixin"]


class _TaskToolRoundMixin:
    """The tools a task agent's turn offers, and running the calls the model makes."""

    # Attribute declarations for type checkers — set by BaseAgent.__init__.
    agent_id: str
    config: dict[str, Any]

    def _build_tool_definitions(self) -> list[dict[str, Any]]:
        """Build normalized tool definitions from the tool registry.

        S-12: filters to only tools in the agent's ``tools`` config; an empty
        list exposes no tools (e.g. planner agent). The rule lives in
        :mod:`agents.tools.tool_list`, which ``_execute_tools`` shares.
        """
        return [
            {
                "name": td.name,
                "description": td.description,
                "parameters": td.parameters,
            }
            for td in offered_tools(self.agent_id, self.config)
        ]

    async def _execute_tools(self, tool_calls: list[ToolCall]) -> list[LLMToolResult]:
        """Execute tool calls sequentially, returning LLM-facing results.

        ISSUE-0151: a tool the agent was not offered gets the same ``Unknown
        tool`` error as one that does not exist, as personas do; neither runs,
        nor does a call whose arguments fail ``admit_call``'s check (RFC 0009).
        """
        results: list[LLMToolResult] = []
        offered = {td.name: td for td in offered_tools(self.agent_id, self.config)}
        for call in tool_calls:
            admitted = admit_call(self.agent_id, offered, call)
            if isinstance(admitted, LLMToolResult):
                results.append(admitted)
                continue
            try:
                result = await admitted.run(**admitted.arguments)
                if result.success:
                    # SF4: JSON for dict/list so the LLM sees {"k": true} not Python repr.
                    content = (
                        json.dumps(result.data)
                        if isinstance(result.data, (dict, list))
                        else str(result.data)
                    )
                    content = maybe_wrap_tool_content(call.name, content)
                else:
                    error_msg = result.error or "Tool failed"
                    # Include error_type when the @tool wrapper captured an
                    # exception (e.g. PermissionError, ValueError) so the LLM
                    # receives structured context about the failure category.
                    if result.error_type:
                        content = f"Tool error ({result.error_type}): {error_msg}"
                    else:
                        content = error_msg
                    # A failure can still carry output — shell_exec's stdout
                    # and stderr on a non-zero exit — and the model needs it
                    # to see what went wrong.
                    if result.data:
                        output = (
                            json.dumps(result.data)
                            if isinstance(result.data, (dict, list))
                            else str(result.data)
                        )
                        content += "\n" + maybe_wrap_tool_content(call.name, output)
                results.append(
                    LLMToolResult(
                        tool_call_id=call.id,
                        content=content,
                        is_error=not result.success,
                    )
                )
            except PermissionError as exc:
                # Defense-in-depth: normally unreachable (the @tool wrapper catches
                # all exceptions), retained for MCP/custom tools that bypass the
                # decorator. SF-06: log full details, return a generic LLM message.
                logger.warning("Permission denied in tool %s: %s", call.name, exc)
                results.append(
                    LLMToolResult(
                        tool_call_id=call.id,
                        content="Permission denied",
                        is_error=True,
                    )
                )
            except Exception as exc:
                # Defense-in-depth: same rationale as PermissionError above.
                logger.warning("Unexpected error in tool %s: %s", call.name, exc)
                results.append(
                    LLMToolResult(
                        tool_call_id=call.id,
                        content="Internal tool error",
                        is_error=True,
                    )
                )
        return results
