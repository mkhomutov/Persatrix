"""
Persatrix Tool System.

Tools are typed functions that agents can invoke. Three tiers:
  - Built-in: bundled with the framework (see builtin.py)
  - Custom: user-defined via @tool decorator (this module)
  - MCP: external MCP server tools (see mcp_bridge.py)
"""

import functools
import inspect
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ToolResult:
    """Result from a tool invocation."""

    success: bool
    data: Any = None
    error: str | None = None
    # PR review: preserving error_type enables distinguishing transient failures
    # (TimeoutError, ConnectionError) from permanent ones (ValueError, PermissionError)
    # for retry and circuit-breaker decisions.
    error_type: str | None = None


@dataclass
class ToolDefinition:
    """Metadata about a registered tool."""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema for parameters
    permissions: list[str] = field(default_factory=list)
    tier: str = "custom"  # "builtin" | "custom" | "mcp"
    func: Callable | None = None


# ─── Global Tool Registry ──────────────────────────────────

_tool_registry: dict[str, ToolDefinition] = {}


def tool(
    name: str | None = None,
    description: str = "",
    permissions: list[str] | None = None,
    tier: str = "custom",
) -> Callable:
    """
    Decorator to register a function as an agent tool.

    Args:
        tier: One of ``"builtin"``, ``"custom"``, ``"mcp"``. Defaults to
              ``"custom"`` for user-defined tools; framework-bundled tools
              in ``builtin.py`` pass ``tier="builtin"``.

    Usage:
        @tool(name="query_database", description="Run a read-only SQL query")
        async def query_database(query: str, database: str = "main") -> ToolResult:
            ...
    """

    def decorator(func: Callable) -> Callable:
        tool_name = name or func.__name__
        sig = inspect.signature(func)

        # Auto-generate parameter schema from type hints
        params = {}
        for param_name, param in sig.parameters.items():
            param_type = "string"  # default
            if param.annotation is int:
                param_type = "integer"
            elif param.annotation is float:
                param_type = "number"
            elif param.annotation is bool:
                param_type = "boolean"

            params[param_name] = {
                "type": param_type,
                "required": param.default is inspect.Parameter.empty,
            }

        tool_def = ToolDefinition(
            name=tool_name,
            description=description or func.__doc__ or "",
            parameters=params,
            permissions=permissions or [],
            tier=tier,
            func=func,  # replaced with wrapper below
        )
        _tool_registry[tool_name] = tool_def

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> ToolResult:
            # TODO: Permission check before execution
            # TODO: Rate limit check
            # TODO: OTEL span creation
            # TODO: Audit logging
            try:
                if inspect.iscoroutinefunction(func):
                    result = await func(*args, **kwargs)
                else:
                    result = func(*args, **kwargs)

                if isinstance(result, ToolResult):
                    return result
                return ToolResult(success=True, data=result)
            except Exception as e:
                logger.exception("Tool '%s' failed", tool_name)
                return ToolResult(
                    success=False,
                    error=str(e),
                    error_type=type(e).__name__,
                )

        # PR-review MF1: Store the wrapper (not the original function) so
        # _execute_tools() goes through the decorator pipeline — permission
        # checks, rate limiting, OTEL spans, and audit logging (v0.2 TODOs)
        # are applied consistently whether calling via module name or registry.
        tool_def.func = wrapper

        return wrapper

    return decorator


def get_tool(name: str) -> ToolDefinition | None:
    """Look up a tool by name."""
    return _tool_registry.get(name)


def list_tools() -> list[ToolDefinition]:
    """List all registered tools."""
    return list(_tool_registry.values())


def clear_registry() -> None:
    """Reset the tool registry. Use in test fixtures for isolation."""
    _tool_registry.clear()
