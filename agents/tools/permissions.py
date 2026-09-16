"""
Permission gate enforcement.

Checks every tool invocation against the agent's permission config.
Deny-by-default: if a permission isn't explicitly granted, it's denied.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)

# A tool whose config scopes it with a list is granted by that list being
# non-empty, as ``filesystem.read`` and ``filesystem.write`` already are
# under their own names. ``shell_exec`` and ``http_request`` ask for
# ``shell:exec`` and ``network:http``, but the agent schema has no ``exec``
# or ``http`` key, so their grant is the list that scopes them (ISSUE-0150).
_GRANTED_BY_LIST: dict[str, str] = {
    "shell:exec": "allowed_commands",
    "network:http": "allow",
}


class PermissionGate:
    """Deny-by-default permission checker for agent tool invocations.

    Initialized with the agent's ``permissions`` block from agents.yaml.
    All checks return False (denied) when the relevant config section is
    missing — this is the deny-by-default posture.
    """

    def __init__(self, permissions: dict[str, Any] | None = None) -> None:
        self._permissions = permissions or {}

    def check(self, permission: str) -> bool:
        """Check if a dotted permission string is granted.

        Format: ``"category:action"`` (e.g. ``"filesystem:read"``).
        Returns True only if the category exists in config and the key
        that grants the action holds a truthy value (non-empty list or
        True). That key is the action's own name, except for the
        list-scoped tools in ``_GRANTED_BY_LIST``.
        """
        parts = permission.split(":", 1)
        if len(parts) != 2:
            logger.warning("Malformed permission string: %s", permission)
            return False

        category, action = parts
        cat_config = self._permissions.get(category)
        if cat_config is None:
            logger.debug("Permission denied (no config for category): %s", permission)
            return False

        value = cat_config.get(_GRANTED_BY_LIST.get(permission, action))
        if not value:
            logger.debug("Permission denied (action not configured): %s", permission)
            return False

        logger.debug("Permission granted: %s", permission)
        return True

    def is_command_allowed(self, args: list[str]) -> bool:
        """Check if a command (as arg list) is in the shell allowlist.

        Uses progressive prefix matching: ``"git diff"`` in the allowlist
        matches ``["git", "diff", "file.py"]`` but not ``["git", "push"]``.
        """
        if not args:
            logger.debug("Command denied (empty args)")
            return False

        shell_config = self._permissions.get("shell")
        if shell_config is None:
            logger.debug("Command denied (no shell config): %s", args)
            return False

        allowed: list[str] = shell_config.get("allowed_commands", [])
        if not allowed:
            logger.debug("Command denied (empty allowlist): %s", args)
            return False

        for pattern in allowed:
            pattern_parts = pattern.split()
            if len(pattern_parts) > len(args):
                continue
            if args[: len(pattern_parts)] == pattern_parts:
                logger.debug("Command allowed by pattern %r: %s", pattern, args)
                return True

        logger.debug("Command denied (no matching pattern): %s", args)
        return False

    def shell_time_limit(self) -> int | None:
        """The most seconds one shell command may run for this agent.

        Read from ``shell.max_execution_seconds``. None when it is unset or
        not a positive whole number; the caller's own ceiling applies then.
        """
        shell_config = self._permissions.get("shell") or {}
        limit = shell_config.get("max_execution_seconds")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            return None
        return limit

    def is_domain_allowed(self, domain: str) -> bool:
        """Check if a network domain is allowed.

        Implements allow-overrides-deny semantics: if a domain appears in
        both the allow and deny lists, it is **allowed**.  This lets configs
        use ``deny: ["*"]`` as a blanket block with specific allow overrides.
        """
        domain = domain.lower()

        net_config = self._permissions.get("network")
        if net_config is None:
            logger.debug("Domain denied (no network config): %s", domain)
            return False

        allow_list: list[str] = [d.lower() for d in net_config.get("allow", [])]
        deny_list: list[str] = [d.lower() for d in net_config.get("deny", [])]

        # Explicit allow takes priority (allow-overrides-deny).
        if domain in allow_list:
            logger.debug("Domain allowed (explicit allow): %s", domain)
            return True

        # Check deny list — wildcard or exact match.
        if "*" in deny_list or domain in deny_list:
            logger.debug("Domain denied (deny list match): %s", domain)
            return False

        # Not in either list: deny by default when a deny list exists,
        # deny when only an allow list exists and domain is not in it.
        if deny_list:
            logger.debug("Domain denied (not in allow, deny list present): %s", domain)
            return False

        if allow_list:
            logger.debug("Domain denied (not in allow list): %s", domain)
            return False

        logger.debug("Domain denied (no allow/deny config): %s", domain)
        return False


# TODO: Implement sub-agent permission inheritance (child <= parent)
# TODO: Implement permission denial logging aggregation
