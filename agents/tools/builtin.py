"""
Built-in tools bundled with the framework.

These tools provide sandboxed filesystem, shell, and HTTP access with
deny-by-default permission enforcement via PermissionGate and PathValidator.
"""

import asyncio
import logging
import shlex
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

from .permissions import PermissionGate
from .registry import ToolResult, tool
from .sandbox import PathValidator

logger = logging.getLogger(__name__)

# Maximum bytes returned from file reads, shell output, and HTTP responses.
# Prevents gRPC 4 MB message size blowout and LLM context waste.
MAX_OUTPUT_BYTES = 102_400  # 100 KB

# Module-level state set by the server at startup via direct assignment
# (e.g. ``builtin.permission_gate = PermissionGate(...)``).
permission_gate: PermissionGate | None = None
path_validator: PathValidator | None = None
workspace_root: Path | None = None


def _require_gate() -> PermissionGate:
    if permission_gate is None:
        raise RuntimeError("Built-in tools not initialized: permission_gate is None")
    return permission_gate


def _require_validator() -> PathValidator:
    if path_validator is None:
        raise RuntimeError("Built-in tools not initialized: path_validator is None")
    return path_validator


def _require_workspace() -> Path:
    if workspace_root is None:
        raise RuntimeError("Built-in tools not initialized: workspace_root is None")
    return workspace_root


def _truncate(text: str) -> str:
    """Truncate text to MAX_OUTPUT_BYTES with an indicator."""
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= MAX_OUTPUT_BYTES:
        return text
    truncated = encoded[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
    return truncated + "\n... [truncated at 100 KB]"


@tool(name="file_read", description="Read the contents of a file", permissions=["filesystem:read"])
async def file_read(path: str) -> ToolResult:
    """Read a file from the sandboxed workspace."""
    gate = _require_gate()
    validator = _require_validator()

    if not gate.check("filesystem:read"):
        return ToolResult(success=False, error="Permission denied: filesystem:read")

    try:
        resolved = validator.validate(path, mode="read")
    except PermissionError as exc:
        return ToolResult(success=False, error=str(exc), error_type="PermissionError")

    try:
        content = resolved.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ToolResult(
            success=False, error=f"File not found: {path}", error_type="FileNotFoundError"
        )
    except OSError as exc:
        return ToolResult(success=False, error=str(exc), error_type="OSError")

    return ToolResult(success=True, data=_truncate(content))


@tool(name="file_write", description="Write content to a file", permissions=["filesystem:write"])
async def file_write(path: str, content: str) -> ToolResult:
    """Write content to a file in the sandboxed workspace."""
    gate = _require_gate()
    validator = _require_validator()

    if not gate.check("filesystem:write"):
        return ToolResult(success=False, error="Permission denied: filesystem:write")

    try:
        resolved = validator.validate(path, mode="write")
    except PermissionError as exc:
        return ToolResult(success=False, error=str(exc), error_type="PermissionError")

    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
    except OSError as exc:
        return ToolResult(success=False, error=str(exc), error_type="OSError")

    return ToolResult(success=True, data=f"Wrote {len(content)} bytes to {path}")


@tool(name="shell_exec", description="Execute a shell command", permissions=["shell:exec"])
async def shell_exec(command: str, timeout: int = 30) -> ToolResult:
    """Execute an allowlisted shell command."""
    gate = _require_gate()
    ws = _require_workspace()

    if not gate.check("shell:exec"):
        return ToolResult(success=False, error="Permission denied: shell:exec")

    try:
        args = shlex.split(command)
    except ValueError as exc:
        return ToolResult(
            success=False, error=f"Invalid command syntax: {exc}", error_type="ValueError"
        )

    if not args:
        return ToolResult(
            success=False,
            error="Empty command",
            error_type="ValueError",
        )

    if not gate.is_command_allowed(args):
        return ToolResult(
            success=False,
            error=f"Command not in allowlist: {args[0]}",
            error_type="PermissionError",
        )

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(ws),
        )
    except FileNotFoundError:
        return ToolResult(
            success=False,
            error=f"Command not found: {args[0]}",
            error_type="FileNotFoundError",
        )

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except TimeoutError:
        # Graceful termination: terminate → 5s grace → kill → reap
        proc.terminate()
        try:
            await asyncio.wait_for(proc.communicate(), timeout=5)
        except TimeoutError:
            proc.kill()
            await proc.wait()
        return ToolResult(
            success=False,
            error=f"Command timed out after {timeout}s",
            error_type="TimeoutError",
        )

    stdout = _truncate(stdout_bytes.decode("utf-8", errors="replace"))
    stderr = _truncate(stderr_bytes.decode("utf-8", errors="replace"))
    exit_code = proc.returncode

    if exit_code != 0:
        return ToolResult(
            success=False,
            data={"stdout": stdout, "stderr": stderr, "exit_code": exit_code},
            error=f"Command exited with code {exit_code}",
        )

    return ToolResult(
        success=True,
        data={"stdout": stdout, "stderr": stderr, "exit_code": 0},
    )


@tool(name="http_request", description="Make an HTTP request", permissions=["network:http"])
async def http_request(url: str, method: str = "GET", body: str = "") -> ToolResult:
    """Make an HTTP request to an allowlisted domain."""
    gate = _require_gate()

    if not gate.check("network:http"):
        return ToolResult(success=False, error="Permission denied: network:http")

    # Validate URL scheme (only http/https allowed).
    try:
        parsed = urlparse(url)
    except Exception as exc:
        return ToolResult(success=False, error=f"Invalid URL: {exc}", error_type="ValueError")

    if parsed.scheme not in ("http", "https"):
        return ToolResult(
            success=False,
            error=f"Unsupported URL scheme: {parsed.scheme!r} (only http/https allowed)",
            error_type="ValueError",
        )

    hostname = parsed.hostname
    if not hostname:
        return ToolResult(success=False, error="Invalid URL: no hostname", error_type="ValueError")

    if not gate.is_domain_allowed(hostname):
        return ToolResult(
            success=False,
            error=f"Domain not in allowlist: {hostname}",
            error_type="PermissionError",
        )

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        ) as session:
            kwargs: dict = {"method": method, "url": url}
            if body and method.upper() in ("POST", "PUT", "PATCH"):
                kwargs["data"] = body
            async with session.request(**kwargs) as resp:
                text = await resp.text()
                return ToolResult(
                    success=True,
                    data={
                        "status": resp.status,
                        "body": _truncate(text),
                        "headers": dict(resp.headers),
                    },
                )
    except aiohttp.ClientError as exc:
        return ToolResult(success=False, error=str(exc), error_type=type(exc).__name__)
    except TimeoutError:
        return ToolResult(success=False, error="HTTP request timed out", error_type="TimeoutError")
