"""
Tests for PermissionGate.

Validates deny-by-default posture, command allowlist with progressive
prefix matching, and network domain allow-overrides-deny semantics.
"""

import pytest

from agents.tools.permissions import PermissionGate


class TestCheck:
    """Tests for the generic check(permission) method."""

    def test_granted_permission(self):
        gate = PermissionGate({"filesystem": {"read": ["/workspace/**"]}})
        assert gate.check("filesystem:read") is True

    def test_denied_permission_no_config(self):
        gate = PermissionGate({})
        assert gate.check("filesystem:read") is False

    def test_denied_permission_missing_category(self):
        gate = PermissionGate({"network": {"allow": ["example.com"]}})
        assert gate.check("filesystem:read") is False

    def test_denied_permission_missing_action(self):
        gate = PermissionGate({"filesystem": {"read": ["/workspace/**"]}})
        assert gate.check("filesystem:write") is False

    def test_denied_permission_empty_action_value(self):
        gate = PermissionGate({"filesystem": {"write": []}})
        assert gate.check("filesystem:write") is False

    def test_denied_when_none_permissions(self):
        gate = PermissionGate(None)
        assert gate.check("filesystem:read") is False

    def test_malformed_permission_string(self):
        gate = PermissionGate({"filesystem": {"read": ["/"]}})
        assert gate.check("no-colon") is False

    def test_truthy_boolean_action(self):
        gate = PermissionGate({"memory": {"read": True}})
        assert gate.check("memory:read") is True

    @pytest.mark.parametrize(
        ("permissions", "permission", "granted"),
        [
            ({"shell": {"allowed_commands": ["pytest"]}}, "shell:exec", True),
            ({"shell": {"allowed_commands": []}}, "shell:exec", False),
            ({"shell": {"exec": True}}, "shell:exec", False),
            ({"network": {"allow": ["api.example.com"]}}, "network:http", True),
            ({"network": {"allow": [], "deny": ["*"]}}, "network:http", False),
            ({"network": {"http": True}}, "network:http", False),
        ],
    )
    def test_list_scoped_tool_is_granted_by_its_list(self, permissions, permission, granted):
        """ISSUE-0150: the agent schema has no ``shell.exec`` or
        ``network.http`` key, so the allowlist that scopes each tool is its
        grant — and a key the schema forbids grants nothing."""
        assert PermissionGate(permissions).check(permission) is granted


class TestIsCommandAllowed:
    """Tests for shell command allowlist with progressive prefix matching."""

    def test_allowed_single_word(self):
        gate = PermissionGate({
            "shell": {"allowed_commands": ["python", "pytest"]}
        })
        assert gate.is_command_allowed(["python", "test.py"]) is True

    def test_allowed_multi_word(self):
        gate = PermissionGate({
            "shell": {"allowed_commands": ["git diff"]}
        })
        assert gate.is_command_allowed(["git", "diff", "file.py"]) is True

    def test_denied_different_subcommand(self):
        gate = PermissionGate({
            "shell": {"allowed_commands": ["git diff"]}
        })
        assert gate.is_command_allowed(["git", "push"]) is False

    def test_denied_no_shell_config(self):
        gate = PermissionGate({})
        assert gate.is_command_allowed(["ls"]) is False

    def test_denied_empty_allowlist(self):
        gate = PermissionGate({"shell": {"allowed_commands": []}})
        assert gate.is_command_allowed(["ls"]) is False

    def test_exact_match_command(self):
        gate = PermissionGate({
            "shell": {"allowed_commands": ["ruff"]}
        })
        assert gate.is_command_allowed(["ruff"]) is True

    def test_pattern_longer_than_args(self):
        gate = PermissionGate({
            "shell": {"allowed_commands": ["git diff --staged"]}
        })
        assert gate.is_command_allowed(["git", "diff"]) is False

    def test_denied_absolute_path_for_bare_name_entry(self):
        """A bare-name entry does NOT admit an absolute path to that name.

        Matching is exact-token (`args[:n] == pattern_parts`), so
        `"python"` admits `python` and nothing else. This is the
        security-relevant half of the rule: if it ever loosened to
        basename matching, an allowlist granting `python` would also
        grant `/tmp/attacker/python`, and the allowlist would stop
        bounding *which* binary runs.

        It is also the reason ISSUE-0129's fix had to touch the fixture
        and not just the call sites — `sys.executable` is an absolute
        path, so allowlisting `"python"` beside it would deny it.
        """
        gate = PermissionGate({
            "shell": {"allowed_commands": ["python"]}
        })
        assert gate.is_command_allowed(["/usr/local/bin/python", "-c", "x"]) is False
        assert gate.is_command_allowed(["/tmp/attacker/python"]) is False
        assert gate.is_command_allowed(["python"]) is True

    def test_multiple_patterns_first_match_wins(self):
        gate = PermissionGate({
            "shell": {"allowed_commands": ["python", "pytest", "ruff", "git diff"]}
        })
        assert gate.is_command_allowed(["pytest", "-v", "--tb=short"]) is True
        assert gate.is_command_allowed(["git", "diff"]) is True
        assert gate.is_command_allowed(["rm", "-rf"]) is False


class TestIsDomainAllowed:
    """Tests for network domain allow-overrides-deny semantics."""

    def test_allowed_domain(self):
        gate = PermissionGate({
            "network": {"allow": ["api.anthropic.com"], "deny": []}
        })
        assert gate.is_domain_allowed("api.anthropic.com") is True

    def test_denied_by_wildcard(self):
        gate = PermissionGate({
            "network": {"allow": ["api.anthropic.com"], "deny": ["*"]}
        })
        assert gate.is_domain_allowed("evil.com") is False

    def test_allow_overrides_wildcard_deny(self):
        """Explicit allow takes priority over wildcard deny."""
        gate = PermissionGate({
            "network": {"allow": ["api.anthropic.com"], "deny": ["*"]}
        })
        assert gate.is_domain_allowed("api.anthropic.com") is True

    def test_allow_overrides_explicit_deny(self):
        """Allow-overrides-deny: domain in both lists is allowed."""
        gate = PermissionGate({
            "network": {
                "allow": ["api.anthropic.com"],
                "deny": ["api.anthropic.com"],
            }
        })
        assert gate.is_domain_allowed("api.anthropic.com") is True

    def test_denied_no_network_config(self):
        gate = PermissionGate({})
        assert gate.is_domain_allowed("example.com") is False

    def test_denied_deny_all_no_allow(self):
        gate = PermissionGate({
            "network": {"allow": [], "deny": ["*"]}
        })
        assert gate.is_domain_allowed("anything.com") is False

    def test_denied_not_in_allow_with_deny_present(self):
        gate = PermissionGate({
            "network": {"allow": ["api.anthropic.com"], "deny": ["*"]}
        })
        assert gate.is_domain_allowed("other.com") is False

    def test_denied_not_in_allow_list_only(self):
        gate = PermissionGate({
            "network": {"allow": ["api.anthropic.com"], "deny": []}
        })
        assert gate.is_domain_allowed("not-listed.com") is False

    def test_denied_empty_config(self):
        gate = PermissionGate({"network": {}})
        assert gate.is_domain_allowed("example.com") is False


class TestShellTimeLimit:
    """``shell.max_execution_seconds``: the most seconds one shell command
    may run for this agent (ISSUE-0150)."""

    @pytest.mark.parametrize(
        ("permissions", "limit"),
        [
            ({"shell": {"max_execution_seconds": 30}}, 30),
            ({"shell": {"allowed_commands": ["ls"]}}, None),
            ({}, None),
            # Not a positive whole number, so ignored and the tool's own
            # ceiling applies. The schema rejects all three.
            ({"shell": {"max_execution_seconds": 0}}, None),
            ({"shell": {"max_execution_seconds": True}}, None),
            ({"shell": {"max_execution_seconds": "30"}}, None),
        ],
    )
    def test_reads_the_configured_limit(self, permissions, limit):
        assert PermissionGate(permissions).shell_time_limit() == limit


class TestCodeWriterPermissions:
    """Integration-style tests using the code-writer agent config from agents.yaml."""

    @pytest.fixture()
    def gate(self):
        return PermissionGate({
            "filesystem": {
                "read": ["/workspace/src/**", "/workspace/tests/**"],
                "write": ["/workspace/src/**", "/workspace/tests/**"],
                "deny": ["/workspace/.env", "**/.git/**"],
            },
            "shell": {
                "allowed_commands": ["python", "pytest", "ruff", "git diff"],
                "max_execution_seconds": 30,
            },
            "network": {
                "allow": ["api.anthropic.com"],
                "deny": ["*"],
            },
        })

    def test_filesystem_read_granted(self, gate):
        assert gate.check("filesystem:read") is True

    def test_filesystem_write_granted(self, gate):
        assert gate.check("filesystem:write") is True

    def test_shell_exec_granted(self, gate):
        assert gate.check("shell:exec") is True

    def test_python_command_allowed(self, gate):
        assert gate.is_command_allowed(["python", "test.py"]) is True

    def test_git_diff_allowed(self, gate):
        assert gate.is_command_allowed(["git", "diff", "main"]) is True

    def test_git_push_denied(self, gate):
        assert gate.is_command_allowed(["git", "push"]) is False

    def test_anthropic_api_allowed(self, gate):
        assert gate.is_domain_allowed("api.anthropic.com") is True

    def test_arbitrary_domain_denied(self, gate):
        assert gate.is_domain_allowed("malicious.com") is False


class TestFollowUpFixes:
    """Tests for PR 2 review follow-up findings."""

    def test_case_insensitive_domain_matching(self):
        """S-02: DNS is case-insensitive (RFC 4343)."""
        gate = PermissionGate({
            "network": {"allow": ["api.anthropic.com"], "deny": ["*"]}
        })
        assert gate.is_domain_allowed("API.ANTHROPIC.COM") is True
        assert gate.is_domain_allowed("Api.Anthropic.Com") is True

    def test_empty_args_returns_false(self):
        """N-04: empty args list is explicitly denied."""
        gate = PermissionGate({
            "shell": {"allowed_commands": ["ls"]}
        })
        assert gate.is_command_allowed([]) is False

    def test_non_wildcard_deny(self):
        """Coverage gap: explicit domain in deny list (not wildcard)."""
        gate = PermissionGate({
            "network": {"allow": [], "deny": ["evil.com"]}
        })
        assert gate.is_domain_allowed("evil.com") is False
        assert gate.is_domain_allowed("other.com") is False
