"""
Sandbox rules for tool execution.

``PathValidator`` keeps ``file_read`` and ``file_write`` to the paths an
agent's permissions allow.

The rest is RFC 0009 Phase 3's limits (the Phases 3–4 plan's PR 4), each
set by the agent's ``resource_limits`` block and each off until it is set,
so a tool behaves as it did before (design point G-3):

- ``ResourceLimiter`` caps the CPU time of a ``shell_exec`` command
  (``shell_cpu_seconds``). The limit is set in the child process before the
  command starts, so the kernel stops a command that spins. Windows has no
  such limit, and there only the wall time applies. The wall time stays
  ``permissions.shell.max_execution_seconds``, which ``shell_exec`` already
  reads; it is adopted here rather than set twice.
- ``OutputSizeLimiter`` caps what one tool call hands the model
  (``tool_output_bytes``), for every tool, in the ``@tool`` wrapper.
  ``file_read``, ``shell_exec`` and ``http_request`` keep their own
  100 KB cut (``MAX_OUTPUT_BYTES`` in ``builtin.py``) either way.

A limit that stops a command, or the output cap, writes a
``tool.resource_limited`` audit line. Like ``tool.arg_invalid``, it is a
structured log record on ``agents.tools.audit``: it reaches the
orchestrator's log stream but not its checksummed audit chain (G-4).

``tool_process_env`` is the environment a process a tool starts gets: the
agent's own, less every ``SECURITY_*`` variable, so a prompt-injected
command cannot read the fleet enrollment secret (G-14).
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import signal
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..observability.audit_record import emit_audit_record
from ..observability.metrics import current_agent_id

if sys.platform != "win32":
    import resource

logger = logging.getLogger(__name__)
_audit_logger = logging.getLogger("agents.tools.audit")

# Use fnmatchcase for deterministic cross-platform behavior.
# fnmatch.fnmatch is case-insensitive on Windows but case-sensitive on
# Linux — agents run in Linux containers per Dockerfile.agent, so we
# enforce case-sensitive matching everywhere.
_fnmatch = fnmatch.fnmatchcase


class PathValidator:
    """Workspace-scoped path restriction for filesystem tools.

    Validates that a requested path resolves to a location within
    the allowed glob patterns and is not blocked by the deny list.
    Deny list takes **unconditional precedence** over allow — a path
    matching both allow and deny is denied.  This is stricter than
    the network domain semantics (where allow overrides deny) because
    filesystem traversal attacks are higher severity.
    """

    def __init__(
        self,
        allow_read: list[str] | None = None,
        allow_write: list[str] | None = None,
        deny: list[str] | None = None,
    ) -> None:
        self._allow_read = allow_read or []
        self._allow_write = allow_write or []
        self._deny = deny or []

    def validate(self, path: str, mode: str = "read") -> Path:
        """Validate and resolve a path against permission rules.

        Args:
            path: The raw path string from the tool invocation.
            mode: ``"read"`` or ``"write"``.

        Returns:
            The resolved absolute ``Path`` if access is permitted.

        Raises:
            PermissionError: If the path is denied or not in the allow list.
            ValueError: If ``mode`` is not ``"read"`` or ``"write"``.
        """
        if mode not in ("read", "write"):
            raise ValueError(f"Invalid mode: {mode!r} (expected 'read' or 'write')")

        resolved = Path(path).resolve()
        resolved_str = str(resolved)

        # Deny list takes unconditional precedence.
        for pattern in self._deny:
            if _fnmatch(resolved_str, pattern):
                logger.warning(
                    "Path denied (deny list match %r): %s", pattern, resolved_str
                )
                raise PermissionError(
                    "Access denied: path is blocked by security policy"
                )

        # Check allow list based on mode.
        allow_list = self._allow_read if mode == "read" else self._allow_write
        if not allow_list:
            logger.warning("Path denied (empty %s allow list): %s", mode, resolved_str)
            raise PermissionError(
                f"Access denied: no {mode} permissions configured"
            )

        for pattern in allow_list:
            if _fnmatch(resolved_str, pattern):
                logger.debug(
                    "Path allowed (%s, pattern %r): %s", mode, pattern, resolved_str
                )
                return resolved

        logger.warning("Path denied (no matching allow pattern): %s", resolved_str)
        raise PermissionError(
            f"Access denied: path is not in {mode} allow list"
        )


# ─── The environment of a tool's process (G-14) ───────────────

#: Every variable whose name starts with this, in any letter case, is taken
#: out of a tool process's environment. The orchestrator's security settings
#: use it, the fleet enrollment secret (``SECURITY_ENROLLMENT_SECRET``) among
#: them; no tool needs any of them.
SCRUBBED_ENV_PREFIX = "SECURITY_"


def tool_process_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment for a process a tool starts: *environ*, less its secrets.

    *environ* defaults to the agent's own. The agent token is held in memory
    only, so it is never in an environment to take out.
    """
    source = os.environ if environ is None else environ
    return {
        name: value for name, value in source.items()
        if not name.upper().startswith(SCRUBBED_ENV_PREFIX)
    }


# ─── The CPU limit on a shell command ─────────────────────────


def cpu_rlimit(seconds: int, current_hard: int) -> tuple[int, int]:
    """The ``RLIMIT_CPU`` pair a child sets for a limit of *seconds*.

    The kernel sends SIGXCPU at the soft limit, and SIGKILL at the hard one
    to a command that ignores it, a second later. A process may not raise
    its hard limit, so a lower one already set (by the container, say)
    stands, and the soft limit never passes it.
    """
    hard = seconds + 1
    if current_hard != resource.RLIM_INFINITY:
        hard = min(hard, current_hard)
    return min(seconds, hard), hard


class ResourceLimiter:
    """The CPU limit on one ``shell_exec`` command, or none.

    The wall-time limit is not here: it stays the agent's
    ``permissions.shell.max_execution_seconds``, as before.
    """

    def __init__(self, cpu_seconds: int | None = None) -> None:
        if cpu_seconds is not None and cpu_seconds < 1:
            raise ValueError("a CPU limit is at least one second")
        self.cpu_seconds = cpu_seconds

    def child_setup(self) -> Callable[[], None] | None:
        """What the child runs before the command starts, or None for nothing.

        None with no limit set, so a command starts exactly as before, and on
        Windows, which has no CPU limit. The function makes one system call
        each way, which is safe in a child forked from a threaded process.
        """
        seconds = self.cpu_seconds
        if seconds is None or sys.platform == "win32":
            return None

        def set_cpu_limit() -> None:
            _, current_hard = resource.getrlimit(resource.RLIMIT_CPU)
            resource.setrlimit(resource.RLIMIT_CPU, cpu_rlimit(seconds, current_hard))

        return set_cpu_limit

    def stopped_by_cpu_limit(self, returncode: int | None) -> bool:
        """Whether a command that ended with *returncode* was stopped by the limit.

        Only SIGXCPU counts. A command that ignores it is killed a second later
        with SIGKILL, which reads as any other kill: its exit code is reported
        as it is, and no audit line is written.
        """
        if self.cpu_seconds is None or returncode is None or sys.platform == "win32":
            return False
        return returncode == -signal.SIGXCPU


# ─── The cap on what a tool returns ───────────────────────────


def _size_label(max_bytes: int) -> str:
    if max_bytes % 1024 == 0:
        return f"{max_bytes // 1024} KB"
    return f"{max_bytes} bytes"


def truncate_text(text: str, max_bytes: int) -> str:
    """*text* cut to *max_bytes* of UTF-8 with a notice, or as it is if it fits.

    The notice names the limit, as ``\\n... [truncated at 100 KB]``. A cut
    never splits a character.
    """
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text
    kept = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return f"{kept}\n... [truncated at {_size_label(max_bytes)}]"


def render_output(data: Any) -> str:
    """A tool's output as the model is shown it: JSON for a dict or a list.

    The same rule as the two tool rounds (``agents/task_tool_round.py`` and
    ``agents/persona_runtime/tool_round.py``) use.
    """
    return json.dumps(data) if isinstance(data, (dict, list)) else str(data)


def _byte_size(text: str) -> int:
    return len(text.encode("utf-8", errors="replace"))


def _longest_text(data: Any) -> int:
    if isinstance(data, str):
        return _byte_size(data)
    if isinstance(data, dict):
        return max((_longest_text(v) for v in data.values()), default=0)
    if isinstance(data, list):
        return max((_longest_text(v) for v in data), default=0)
    return 0


class OutputSizeLimiter:
    """Caps what one tool call hands the model at *max_bytes*.

    The size is that of the output as the model is shown it. A text over the
    cap is cut with :func:`truncate_text`'s notice. A dict or a list keeps its
    shape: every text in it longer than one common length is cut to that
    length, the longest length that makes the whole fit, so a long stdout
    does not cost the model ``shell_exec``'s stderr and exit code. One with
    too many fields for any such cut to fit is cut as its shown text.
    """

    def __init__(self, max_bytes: int) -> None:
        if max_bytes < 1:
            raise ValueError("an output cap is at least one byte")
        self.max_bytes = max_bytes

    def limit(self, data: Any) -> tuple[Any, int | None]:
        """*data* cut to fit, and its size before the cut; None if it fit.

        An output ``json.dumps`` refuses is returned as it is, to fail in the
        tool round as it did before.
        """
        try:
            shown = render_output(data)
        except (TypeError, ValueError):
            return data, None
        size = _byte_size(shown)
        if size <= self.max_bytes:
            return data, None
        if isinstance(data, (dict, list)):
            fitted = self._fit_texts(data)
            if fitted is not None:
                return fitted, size
        return truncate_text(shown, self.max_bytes), size

    def _cut_texts(self, data: Any, length: int) -> Any:
        if isinstance(data, str):
            if _byte_size(data) <= length:
                return data
            kept = data.encode("utf-8", errors="replace")[:length].decode("utf-8", errors="ignore")
            return f"{kept}\n... [truncated at {_size_label(self.max_bytes)}]"
        if isinstance(data, dict):
            return {k: self._cut_texts(v, length) for k, v in data.items()}
        if isinstance(data, list):
            return [self._cut_texts(v, length) for v in data]
        return data

    def _fits(self, data: Any) -> bool:
        return _byte_size(render_output(data)) <= self.max_bytes

    def _fit_texts(self, data: dict[str, Any] | list[Any]) -> Any:
        # No text cut longer than the cap fits, so cut there once first and
        # search the rest on that smaller copy.
        data = self._cut_texts(data, self.max_bytes)
        low, high = 0, min(_longest_text(data), self.max_bytes)
        if not self._fits(self._cut_texts(data, low)):
            return None
        # The texts uncut never fit, nor do any cut at the cap. Cutting one
        # more byte can lengthen a text by its notice, so this finds a length
        # that fits, not always the longest.
        while high - low > 1:
            middle = (low + high) // 2
            if self._fits(self._cut_texts(data, middle)):
                low = middle
            else:
                high = middle
        return self._cut_texts(data, low)


# ─── The resource_limits block and its audit line ─────────────

#: ``shell_cpu_seconds`` is at most the wall-time ceiling: a one-thread
#: command can spend no more CPU than that. ``tool_output_bytes`` is at most
#: the built-in tools' own cut, which it can lower and never raise.
_LIMIT_RANGES: dict[str, tuple[int, int]] = {
    "shell_cpu_seconds": (1, 300),
    "tool_output_bytes": (1, 102_400),
}


@dataclass(frozen=True)
class ResourceLimits:
    """One agent's limits, read from its ``resource_limits`` block."""

    shell: ResourceLimiter
    output: OutputSizeLimiter | None


def read_resource_limits(block: object) -> ResourceLimits:
    """Read an agent's ``resource_limits`` block; None or absent sets no limit.

    Raises ``ValueError`` naming the key for a value
    ``schemas/agent.schema.json`` refuses: a limit an operator set must not
    be dropped quietly, so a bad one stops the agent at startup.
    """
    if block is None:
        block = {}
    if not isinstance(block, dict):
        raise ValueError("resource_limits must be a mapping")
    unknown = sorted(set(block) - set(_LIMIT_RANGES))
    if unknown:
        raise ValueError(f"resource_limits does not take {', '.join(map(str, unknown))}")
    values: dict[str, int | None] = {}
    for key, (low, high) in _LIMIT_RANGES.items():
        value = block.get(key)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high
        ):
            raise ValueError(f"resource_limits.{key} must be a whole number from {low} to {high}")
        values[key] = value
    output_bytes = values["tool_output_bytes"]
    return ResourceLimits(
        shell=ResourceLimiter(values["shell_cpu_seconds"]),
        output=None if output_bytes is None else OutputSizeLimiter(output_bytes),
    )


def emit_resource_limited(tool_name: str, limit: str, **fields: Any) -> None:
    """Write the ``tool.resource_limited`` audit line for *limit* hit by *tool_name*."""
    emit_audit_record(
        _audit_logger, "tool.resource_limited",
        agent_id=current_agent_id(), tool_name=tool_name, limit=limit, **fields,
    )
