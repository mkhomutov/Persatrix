"""
The permission gate: what an agent's config grants its tools.

Deny-by-default: a permission the agent's ``permissions`` block does not
grant is denied. The gate is asked in two places:

- Before a tool runs. :func:`agents.tools.tool_list.admit_call`, the one
  admission function both agent kinds use, refuses a model's call unless
  the agent's config grants every permission the tool declares with
  ``@tool(permissions=[...])`` (ISSUE-0214).
- Inside each built-in tool, which asks again and then asks what a declared
  permission cannot say: which path, which command, which domain.

A tool that declares no permission is held only by the agent's ``tools``
list, and a tool function called directly as Python passes through neither
check but its own.

An agent's config can grant the seven permissions in
:data:`GRANTABLE_PERMISSIONS` and no other: the agent schema
(``schemas/agent.schema.json``) has five permission categories and allows
no other key. So a tool cannot have a permission of its own yet, and
``@tool`` refuses to register one that declares it
(:func:`declared_permissions_error`).
"""

import logging
from collections.abc import Iterable, Mapping
from typing import Any

logger = logging.getLogger(__name__)

# Each permission an agent's config can grant, with the key of its category
# that grants it: in a config the schema accepts, a non-empty list or
# ``true`` (``PermissionGate.check`` says what any other value does).
# ``shell_exec`` and ``http_request`` ask for ``shell:exec`` and
# ``network:http``, but the agent schema has no ``exec`` or ``http`` key,
# so their grant is the list that scopes them (ISSUE-0150). The keys left
# out (``filesystem.deny``, ``network.deny``,
# ``shell.max_execution_seconds``) scope a tool and turn nothing on.
# ``tests/unit/python/test_tool_permission_names.py`` holds this table to
# the schema.
_GRANTED_BY: dict[str, str] = {
    "filesystem:read": "read",
    "filesystem:write": "write",
    "network:http": "allow",
    "shell:exec": "allowed_commands",
    "memory:read": "read",
    "memory:write": "write",
    "channels:recall": "recall",
}

#: Every permission an agent's config can grant, and so every one a tool
#: may declare.
GRANTABLE_PERMISSIONS: frozenset[str] = frozenset(_GRANTED_BY)

#: The categories of the agent schema's ``permissions`` block.
PERMISSION_CATEGORIES: frozenset[str] = frozenset(
    permission.split(":", 1)[0] for permission in _GRANTED_BY
)


def declared_permissions_error(tool_name: str, declared: object) -> str | None:
    """Why a tool may not declare *declared*, or None when it may.

    A tool may declare only permissions an agent's config can grant. Any
    other name could never be granted, so the tool would be refused on
    every call; saying so when the tool is defined is kinder than that.
    """
    if isinstance(declared, str) or not isinstance(declared, Iterable):
        return (
            f"Tool {tool_name!r}: permissions must be a list of permission names, "
            f"not {type(declared).__name__}"
        )
    for permission in declared:
        if not isinstance(permission, str):
            return (
                f"Tool {tool_name!r}: permissions must be a list of permission names, "
                f"and {permission!r} is not one"
            )
        if permission in GRANTABLE_PERMISSIONS:
            continue
        can_declare = ", ".join(sorted(GRANTABLE_PERMISSIONS))
        if permission.split(":", 1)[0] in PERMISSION_CATEGORIES:
            return (
                f"Tool {tool_name!r} declares the permission {permission!r}, and "
                f"no agent's config can grant it. A tool can declare: {can_declare}"
            )
        return (
            f"Tool {tool_name!r} declares the permission {permission!r}: a tool "
            "cannot have a permission of its own yet, because the agent schema "
            f"has no place to grant one. A tool can declare: {can_declare}"
        )
    return None


class PermissionGate:
    """Deny-by-default permission checker for agent tool invocations.

    Initialized with the agent's ``permissions`` block from agents.yaml.
    All checks return False (denied) when the relevant config section is
    missing — this is the deny-by-default posture.
    """

    def __init__(self, permissions: Mapping[str, Any] | None = None) -> None:
        # Nothing checks the agent schema at start-up, so the block can be
        # any YAML value; one that is not a mapping grants nothing.
        self._permissions: Mapping[str, Any] = (
            permissions if isinstance(permissions, Mapping) else {}
        )

    def check(self, permission: str) -> bool:
        """Whether the agent's config grants *permission*.

        Format: ``"category:action"`` (e.g. ``"filesystem:read"``).
        Returns True only for one of :data:`GRANTABLE_PERMISSIONS` whose
        category is in the config and whose granting key holds a truthy
        value. The agent schema allows a list or ``true`` there, and a
        non-empty list or ``true`` grants. So does any other truthy value,
        which the schema refuses and nothing checks at start-up: the text
        ``"false"`` grants (ISSUE-0289). Any other name is denied whatever
        the config holds: ``network:deny`` is not granted by a deny list.
        """
        parts = permission.split(":", 1)
        if len(parts) != 2:
            logger.warning("Malformed permission string: %s", permission)
            return False

        granting_key = _GRANTED_BY.get(permission)
        if granting_key is None:
            logger.debug("Permission denied (no config can grant it): %s", permission)
            return False

        cat_config = self._permissions.get(parts[0])
        if not isinstance(cat_config, Mapping):
            logger.debug("Permission denied (no config for category): %s", permission)
            return False

        if not cat_config.get(granting_key):
            logger.debug("Permission denied (action not configured): %s", permission)
            return False

        logger.debug("Permission granted: %s", permission)
        return True

    def missing(self, declared: Iterable[str]) -> str | None:
        """The first of *declared* the agent's config does not grant, or None.

        *declared* is what a tool declares with ``@tool(permissions=[...])``;
        the tool may run for this agent only when this returns None.
        """
        for permission in declared:
            if not isinstance(permission, str) or not self.check(permission):
                return str(permission)
        return None

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


# Not built: the rule that a sub-agent gets no more than its parent
# (ISSUE-0285). Nothing reaches the sub-agent spawner yet.
# TODO: Implement permission denial logging aggregation
