"""
Built-in tools bundled with the framework.

These tools provide sandboxed filesystem, shell, and HTTP access with
deny-by-default permission enforcement via PermissionGate and PathValidator.
Also provides agent-initiated memory tools (notes) via closure-based factory.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import aiohttp

from .permissions import PermissionGate
from .registry import ToolDefinition, ToolResult, get_tool, tool
from .sandbox import PathValidator

if TYPE_CHECKING:
    from ..memory.episodic import EpisodicMemory

logger = logging.getLogger(__name__)

# SF-02: only forward safe response headers to the LLM. Headers like
# Set-Cookie, Server, X-Powered-By can leak infrastructure details or
# session tokens into LLM context.
_SAFE_RESPONSE_HEADERS = frozenset({
    "content-type", "content-length", "location", "date",
    "cache-control", "etag", "last-modified",
})


# Maximum bytes returned from file reads, shell output, and HTTP responses.
# Prevents gRPC 4 MB message size blowout and LLM context waste.
MAX_OUTPUT_BYTES = 102_400  # 100 KB

# Module-level state set by the server at startup via direct assignment
# (e.g. ``builtin.permission_gate = PermissionGate(...)``).
# TODO(v0.2): refactor to dependency injection for multi-agent hosting
# where each agent needs its own PermissionGate/PathValidator instance.
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
    truncated = encoded[:MAX_OUTPUT_BYTES].decode("utf-8", errors="ignore")
    return truncated + "\n... [truncated at 100 KB]"


@tool(
    name="file_read",
    description="Read the contents of a file",
    permissions=["filesystem:read"],
    tier="builtin",
)
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
    except UnicodeDecodeError:
        return ToolResult(
            success=False, error=f"Cannot read binary file: {path}", error_type="UnicodeDecodeError"
        )
    except OSError as exc:
        return ToolResult(success=False, error=str(exc), error_type="OSError")

    return ToolResult(success=True, data=_truncate(content))


@tool(
    name="file_write",
    description="Write content to a file",
    permissions=["filesystem:write"],
    tier="builtin",
)
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

    return ToolResult(success=True, data=f"Wrote {len(content.encode('utf-8'))} bytes to {path}")


# Maximum timeout in seconds for shell commands. Prevents LLM from
# requesting effectively infinite timeouts via the exposed parameter.
MAX_TIMEOUT_SECONDS = 300


@tool(
    name="shell_exec",
    description="Execute a shell command",
    permissions=["shell:exec"],
    tier="builtin",
)
async def shell_exec(command: str, timeout: int = 30) -> ToolResult:
    """Execute an allowlisted shell command."""
    gate = _require_gate()
    ws = _require_workspace()

    if not gate.check("shell:exec"):
        return ToolResult(success=False, error="Permission denied: shell:exec")

    # Clamp timeout to [1, MAX_TIMEOUT_SECONDS] to prevent resource abuse.
    # timeout=0 wastes a process spawn; large values create runaway processes.
    timeout = max(1, min(timeout, MAX_TIMEOUT_SECONDS))

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
            stdin=asyncio.subprocess.DEVNULL,  # M-02: prevent blocking on interactive prompts
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
        # Three-phase shutdown: terminate → 5s grace period → kill → reap
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


# HTTP methods allowed for tool invocations. DELETE is excluded to prevent
# accidental data loss on allowlisted API endpoints.
ALLOWED_HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH"})


@tool(
    name="http_request",
    description="Make an HTTP request",
    permissions=["network:http"],
    tier="builtin",
)
async def http_request(url: str, method: str = "GET", body: str = "") -> ToolResult:
    """Make an HTTP request to an allowlisted domain."""
    gate = _require_gate()

    if not gate.check("network:http"):
        return ToolResult(success=False, error="Permission denied: network:http")

    # Normalize once to avoid repeated .upper() calls (review N-01).
    method = method.upper()

    if method not in ALLOWED_HTTP_METHODS:
        return ToolResult(
            success=False,
            error=f"HTTP method not allowed: {method!r} (allowed: GET, POST, PUT, PATCH)",
            error_type="ValueError",
        )

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

    # TODO(v0.2): reuse aiohttp session across tool invocations to avoid
    # per-request connection setup overhead (~50ms) in sub-agent patterns.
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        ) as session:
            kwargs: dict = {"method": method, "url": url}
            if body and method in ("POST", "PUT", "PATCH"):
                kwargs["data"] = body
                # Default to JSON since LLM-generated payloads are overwhelmingly
                # JSON. v0.2 will add an optional ``headers`` parameter for full
                # control (review S-01).
                kwargs["headers"] = {"Content-Type": "application/json"}
            # Disable redirect following to prevent SSRF via open-redirect on
            # allowlisted domains. The LLM can re-issue a request to the
            # redirect target after domain re-validation (review M-01).
            async with session.request(**kwargs, allow_redirects=False) as resp:
                text = await resp.text()
                # SF-02: filter to safe headers only — prevents leaking
                # Set-Cookie, Server, X-Powered-By, etc. to the LLM.
                safe_headers = {
                    k: v for k, v in resp.headers.items()
                    if k.lower() in _SAFE_RESPONSE_HEADERS
                }
                return ToolResult(
                    success=True,
                    data={
                        "status": resp.status,
                        "body": _truncate(text),
                        "headers": safe_headers,
                    },
                )
    except aiohttp.ClientError as exc:
        return ToolResult(success=False, error=str(exc), error_type=type(exc).__name__)
    except TimeoutError:
        return ToolResult(success=False, error="HTTP request timed out", error_type="TimeoutError")


# ─── Memory Tools (Agent-Initiated Notes) ──────────────────


def create_memory_tools(
    memory: EpisodicMemory,
    gate: PermissionGate,
    *,
    max_notes: int = 500,
    auto_reflect_after: int = 0,
) -> list[ToolDefinition]:
    """Create closure-based memory tools bound to a specific EpisodicMemory instance.

    The ``agent_id`` and DB connection are captured in the closure — they are
    NOT controllable by the LLM.

    Returns a list of registered ToolDefinition objects.
    """
    tools: list[ToolDefinition] = []

    @tool(
        name="store_note",
        description="Store a note for future reference",
        permissions=["memory:write"],
        tier="builtin",
    )
    async def store_note(topic: str, content: str, tags: str = "") -> ToolResult:
        """Store a note with a topic and content. Tags is a comma-separated string."""
        if not gate.check("memory:write"):
            return ToolResult(success=False, error="Permission denied: memory:write")
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
        try:
            note_id = await memory.store_note(
                topic=topic, content=content, tags=tag_list, max_notes=max_notes,
            )
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc), error_type="ValueError")
        return ToolResult(success=True, data={"note_id": note_id, "topic": topic})

    @tool(
        name="recall_notes",
        description="Search stored notes by query",
        permissions=["memory:read"],
        tier="builtin",
    )
    async def recall_notes(query: str = "", limit: int = 10) -> ToolResult:
        """Search notes. Empty query returns most recent notes."""
        if not gate.check("memory:read"):
            return ToolResult(success=False, error="Permission denied: memory:read")
        try:
            notes = await memory.recall_notes(query=query, limit=limit)
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc), error_type="ValueError")
        return ToolResult(
            success=True,
            data=[
                {
                    "id": n.id,
                    "topic": n.topic,
                    "content": n.content,
                    "tags": n.tags,
                    "access_count": n.access_count,
                }
                for n in notes
            ],
        )

    @tool(
        name="update_note",
        description="Update the content of an existing note",
        permissions=["memory:write"],
        tier="builtin",
    )
    async def update_note(note_id: str, content: str) -> ToolResult:
        """Update a note's content. Topic and tags are preserved."""
        if not gate.check("memory:write"):
            return ToolResult(success=False, error="Permission denied: memory:write")
        try:
            found = await memory.update_note(note_id=note_id, content=content)
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc), error_type="ValueError")
        if not found:
            return ToolResult(success=False, error=f"Note not found: {note_id}")
        return ToolResult(success=True, data={"note_id": note_id, "updated": True})

    @tool(
        name="delete_note",
        description="Delete a stored note",
        permissions=["memory:write"],
        tier="builtin",
    )
    async def delete_note(note_id: str) -> ToolResult:
        """Delete a note by ID."""
        if not gate.check("memory:write"):
            return ToolResult(success=False, error="Permission denied: memory:write")
        found = await memory.delete_note(note_id=note_id)
        if not found:
            return ToolResult(success=False, error=f"Note not found: {note_id}")
        return ToolResult(success=True, data={"note_id": note_id, "deleted": True})

    # Collect registered tool definitions
    for name in ("store_note", "recall_notes", "update_note", "delete_note"):
        td = get_tool(name)
        if td is not None:
            tools.append(td)

    return tools


async def check_auto_reflect(
    memory: EpisodicMemory,
    auto_reflect_after: int,
) -> str | None:
    """Increment the interaction counter and return a nudge if threshold reached.

    Returns a system prompt nudge string if ``auto_reflect_after > 0`` and the
    counter has reached the threshold, otherwise ``None``.  Resets the counter
    after firing.
    """
    if auto_reflect_after <= 0:
        return None
    count = await memory.increment_interaction_count()
    if count >= auto_reflect_after:
        await memory.reset_interaction_count()
        return (
            "You have processed several interactions since your last reflection. "
            "Consider using store_note to record any new insights, patterns, or "
            "important context you've observed."
        )
    return None
