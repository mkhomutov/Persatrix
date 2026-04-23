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
from typing import Any, get_type_hints

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from ..observability.spans import (
    TOOL_EXECUTE_SPAN,
    apply_redaction,
    tool_payload_capture_mode,
)

logger = logging.getLogger(__name__)
_tracer = trace.get_tracer(__name__)


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
        # Resolve postponed annotations (PEP 563 / ``from __future__ import annotations``)
        # so built-in scalar types map correctly to JSON Schema.
        # Log resolution failures at DEBUG — get_type_hints() can raise on forward
        # references to runtime-only symbols or malformed annotations; the fallback
        # to raw param.annotation is safe but silent, so surface the cause for
        # operators investigating "type annotation defaulted to string" warnings.
        try:
            resolved_hints = get_type_hints(func)
        except Exception as exc:
            logger.debug(
                "get_type_hints failed for %s, falling back to raw annotations: %s",
                getattr(func, "__qualname__", repr(func)),
                exc,
            )
            resolved_hints = {}

        # Auto-generate parameter schema from type hints.
        # Produces a valid JSON Schema object for use as tool input_schema.
        known_types = {int: "integer", float: "number", bool: "boolean", str: "string"}
        properties: dict[str, Any] = {}
        required: list[str] = []
        for param_name, param in sig.parameters.items():
            annotation = resolved_hints.get(param_name, param.annotation)
            if annotation in known_types:
                param_type = known_types[annotation]
            elif annotation is inspect.Parameter.empty:
                param_type = "string"
            else:
                # (PR #71 deep-review §2.4.7): warn on unrecognized annotations
                # so future tools with complex types (list[str], Path, etc.) don't
                # silently degrade to "string" without the author noticing.
                param_type = "string"
                logger.warning(
                    "Tool %r param %r has unrecognized type annotation %r, "
                    "defaulting to JSON Schema 'string'",
                    tool_name,
                    param_name,
                    annotation,
                )

            properties[param_name] = {"type": param_type}
            if param.default is inspect.Parameter.empty:
                required.append(param_name)

        params: dict[str, Any] = {
            "type": "object",
            "properties": properties,
        }
        if required:
            params["required"] = required

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
            # TODO: Audit logging
            with _tracer.start_as_current_span(
                TOOL_EXECUTE_SPAN,
                attributes={"tool.name": tool_name},
            ) as span:
                # Opt-in payload capture (RFC 0019 § D).  Default mode is
                # ``none`` so no payload data leaks into traces unless the
                # operator opts in via ``PERSATRIX_TRACE_TOOL_PAYLOADS``.
                # ``metadata`` emits arg names + types only; ``full`` runs
                # values through the RFC 0018 redactor (a single secrets
                # policy code path serves both logs and span attributes).
                mode = tool_payload_capture_mode()
                if mode != "none":
                    payload: dict[str, Any] = {}
                    for i, value in enumerate(args):
                        payload[f"arg{i}"] = value
                    payload.update(kwargs)
                    if mode == "metadata":
                        for key, value in payload.items():
                            span.set_attribute(
                                f"tool.arguments.{key}.type",
                                type(value).__name__,
                            )
                    else:  # full
                        redacted = apply_redaction(payload)
                        for key, value in redacted.items():
                            span.set_attribute(
                                f"tool.arguments.{key}", str(value),
                            )
                try:
                    if inspect.iscoroutinefunction(func):
                        result = await func(*args, **kwargs)
                    else:
                        result = func(*args, **kwargs)

                    if isinstance(result, ToolResult):
                        span.set_attribute("tool.success", result.success)
                        if not result.success:
                            span.set_status(
                                Status(StatusCode.ERROR, result.error or ""),
                            )
                        return result
                    span.set_attribute("tool.success", True)
                    return ToolResult(success=True, data=result)
                except Exception as e:
                    logger.exception("Tool '%s' failed", tool_name)
                    span.record_exception(e)
                    span.set_attribute("tool.success", False)
                    span.set_status(Status(StatusCode.ERROR, str(e)))
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
