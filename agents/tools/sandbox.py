"""
Resource limits and sandboxing for tool execution.

Enforces: max execution time, max output size, path restrictions.
"""

import fnmatch
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

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


# TODO: Implement ResourceLimiter
# TODO: Implement OutputSizeLimiter
