"""
Built-in tools bundled with the framework.

These tools are always available and require no external dependencies.
"""

from .registry import tool, ToolResult


@tool(name="file_read", description="Read the contents of a file", permissions=["filesystem:read"])
async def file_read(path: str) -> ToolResult:
    """Read a file from the sandboxed workspace."""
    # TODO: Implement with path validation (glob allowlist)
    # TODO: Enforce filesystem permission check
    raise NotImplementedError


@tool(name="file_write", description="Write content to a file", permissions=["filesystem:write"])
async def file_write(path: str, content: str) -> ToolResult:
    """Write content to a file in the sandboxed workspace."""
    # TODO: Implement with path validation
    # TODO: Enforce filesystem permission check
    raise NotImplementedError


@tool(name="shell_exec", description="Execute a shell command", permissions=["shell:exec"])
async def shell_exec(command: str, timeout: int = 30) -> ToolResult:
    """Execute an allowlisted shell command."""
    # TODO: Implement with command allowlist validation
    # TODO: Implement argument sanitization (no shell=True)
    # TODO: Enforce timeout
    raise NotImplementedError


@tool(name="http_request", description="Make an HTTP request", permissions=["network:http"])
async def http_request(url: str, method: str = "GET", body: str = "") -> ToolResult:
    """Make an HTTP request to an allowlisted domain."""
    # TODO: Implement with domain allowlist
    # TODO: Enforce network permission check
    raise NotImplementedError
