"""
Tests for PathValidator.

Validates workspace-scoped path restriction with deny-wins semantics,
symlink resolution, and mode-based access control.
"""

import os
import platform
from pathlib import Path

import pytest
import yaml

from agents.tools.sandbox import PathValidator

_REPO_ROOT = Path(__file__).resolve().parents[3]


class TestValidateRead:
    """Tests for read-mode path validation."""

    def test_valid_read_path(self, tmp_path):
        target = tmp_path / "src" / "main.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("print('hello')")

        validator = PathValidator(
            allow_read=[str(tmp_path / "src" / "**")],
        )
        result = validator.validate(str(target), mode="read")
        assert result == target.resolve()

    def test_denied_read_outside_allow(self, tmp_path):
        validator = PathValidator(
            allow_read=[str(tmp_path / "src" / "**")],
        )
        with pytest.raises(PermissionError, match="not in read allow list"):
            validator.validate(str(tmp_path / "secrets" / "key.pem"), mode="read")

    def test_denied_read_empty_allowlist(self):
        validator = PathValidator(allow_read=[])
        with pytest.raises(PermissionError, match="no read permissions configured"):
            validator.validate("/some/file.txt", mode="read")


class TestValidateWrite:
    """Tests for write-mode path validation."""

    def test_valid_write_path(self, tmp_path):
        validator = PathValidator(
            allow_write=[str(tmp_path / "src" / "**")],
        )
        result = validator.validate(str(tmp_path / "src" / "new.py"), mode="write")
        assert result == (tmp_path / "src" / "new.py").resolve()

    def test_denied_write_to_read_only(self, tmp_path):
        validator = PathValidator(
            allow_read=[str(tmp_path / "**")],
            allow_write=[],
        )
        with pytest.raises(PermissionError, match="no write permissions configured"):
            validator.validate(str(tmp_path / "file.py"), mode="write")


class TestDenyList:
    """Tests for deny-list precedence."""

    def test_deny_wins_over_allow(self, tmp_path):
        """Path in both allow and deny lists is denied (deny-wins semantics)."""
        env_file = tmp_path / ".env"
        env_file.write_text("SECRET=foo")

        validator = PathValidator(
            allow_read=[str(tmp_path / "**")],
            deny=[str(tmp_path / ".env")],
        )
        with pytest.raises(PermissionError, match="blocked by security policy"):
            validator.validate(str(env_file), mode="read")

    def test_deny_glob_pattern(self, tmp_path):
        git_file = tmp_path / ".git" / "config"
        git_file.parent.mkdir(parents=True, exist_ok=True)
        git_file.write_text("[core]")

        validator = PathValidator(
            allow_read=[str(tmp_path / "**")],
            deny=[str(tmp_path / ".git" / "**")],
        )
        with pytest.raises(PermissionError, match="blocked by security policy"):
            validator.validate(str(git_file), mode="read")

    def test_no_deny_match_allows(self, tmp_path):
        safe_file = tmp_path / "src" / "app.py"
        safe_file.parent.mkdir(parents=True, exist_ok=True)
        safe_file.write_text("app = Flask(__name__)")

        validator = PathValidator(
            allow_read=[str(tmp_path / "src" / "**")],
            deny=[str(tmp_path / ".env")],
        )
        result = validator.validate(str(safe_file), mode="read")
        assert result == safe_file.resolve()


class TestPathTraversal:
    """Tests for path traversal attack prevention."""

    def test_traversal_resolved_outside_allow(self, tmp_path):
        """Path with .. that resolves outside workspace is denied."""
        validator = PathValidator(
            allow_read=[str(tmp_path / "workspace" / "**")],
        )
        traversal_path = str(tmp_path / "workspace" / ".." / ".." / "etc" / "passwd")
        with pytest.raises(PermissionError, match="not in read allow list"):
            validator.validate(traversal_path, mode="read")

    def test_traversal_that_stays_inside(self, tmp_path):
        """Path with .. that still resolves inside workspace is allowed."""
        target = tmp_path / "workspace" / "src" / "utils.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# utils")

        validator = PathValidator(
            allow_read=[str(tmp_path / "workspace" / "**")],
        )
        traversal_path = str(
            tmp_path / "workspace" / "src" / "sub" / ".." / "utils.py"
        )
        result = validator.validate(traversal_path, mode="read")
        assert result == target.resolve()


class TestSymlinkResolution:
    """Tests for symlink-following path validation."""

    @pytest.mark.skipif(
        platform.system() == "Windows" and not os.environ.get("CI"),
        reason="Symlink creation may require elevated privileges on Windows",
    )
    def test_symlink_outside_workspace_denied(self, tmp_path):
        """Symlink pointing outside workspace is denied after resolution."""
        outside = tmp_path / "outside" / "secret.txt"
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.write_text("secret data")

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        link = workspace / "sneaky.txt"
        link.symlink_to(outside)

        validator = PathValidator(
            allow_read=[str(workspace / "**")],
        )
        with pytest.raises(PermissionError, match="not in read allow list"):
            validator.validate(str(link), mode="read")

    @pytest.mark.skipif(
        platform.system() == "Windows" and not os.environ.get("CI"),
        reason="Symlink creation may require elevated privileges on Windows",
    )
    def test_symlink_inside_workspace_allowed(self, tmp_path):
        """Symlink pointing to allowed location within workspace is fine."""
        real = tmp_path / "workspace" / "src" / "real.py"
        real.parent.mkdir(parents=True, exist_ok=True)
        real.write_text("# real file")

        link = tmp_path / "workspace" / "src" / "alias.py"
        link.symlink_to(real)

        validator = PathValidator(
            allow_read=[str(tmp_path / "workspace" / "**")],
        )
        result = validator.validate(str(link), mode="read")
        assert result == real.resolve()


class TestInvalidMode:
    def test_invalid_mode_raises_value_error(self):
        validator = PathValidator(allow_read=["/**"])
        with pytest.raises(ValueError, match="Invalid mode"):
            validator.validate("/some/file", mode="execute")


class TestCodeWriterPaths:
    """Integration-style tests using code-writer agent config patterns."""

    @pytest.fixture()
    def validator(self, tmp_path):
        workspace = tmp_path / "workspace"
        (workspace / "src").mkdir(parents=True)
        (workspace / "tests").mkdir(parents=True)
        (workspace / ".git").mkdir(parents=True)
        (workspace / ".env").write_text("SECRET=x")
        return PathValidator(
            allow_read=[
                str(workspace / "src" / "**"),
                str(workspace / "tests" / "**"),
            ],
            allow_write=[
                str(workspace / "src" / "**"),
                str(workspace / "tests" / "**"),
            ],
            deny=[
                str(workspace / ".env"),
                str(workspace / ".git" / "**"),
            ],
        ), workspace

    def test_read_src(self, validator):
        v, ws = validator
        f = ws / "src" / "app.py"
        f.write_text("app")
        assert v.validate(str(f), mode="read") == f.resolve()

    def test_write_tests(self, validator):
        v, ws = validator
        f = ws / "tests" / "test_app.py"
        assert v.validate(str(f), mode="write") == f.resolve()

    def test_deny_env(self, validator):
        v, ws = validator
        with pytest.raises(PermissionError, match="blocked by security policy"):
            v.validate(str(ws / ".env"), mode="read")

    def test_deny_git(self, validator):
        v, ws = validator
        with pytest.raises(PermissionError, match="blocked by security policy"):
            v.validate(str(ws / ".git" / "config"), mode="read")


def _allows(validator: PathValidator, path: Path, mode: str) -> bool:
    try:
        validator.validate(str(path), mode=mode)
    except PermissionError:
        return False
    return True


class TestHowARuleMatches:
    """ISSUE-0216: a rule is matched against the whole resolved path with
    ``fnmatchcase``, where ``*`` matches ``/`` too. The agent schema and the
    spec state this rule; these pin it, so a change to the matching cannot
    pass unnoticed."""

    @pytest.mark.parametrize(
        ("rule", "path", "matches"),
        [
            # A single * crosses folders: it is not a "top level only" glob.
            ("*.py", "setup.py", True),
            ("*.py", "deploy/hooks/post_install.py", True),
            ("*.md", ".github/workflows/notes.md", True),
            ("src/*", "src/a/b/c.py", True),
            # ** means no more than *.
            ("src/**", "src/a/b/c.py", True),
            # Neither covers the folder's own path, nor a sibling's.
            ("src/**", "src", False),
            ("src/**", "srcs/x.py", False),
            # A rule with no wildcard is one exact path.
            ("setup.py", "setup.py", True),
            ("setup.py", "sub/setup.py", False),
            ("", "malicious.sh", False),
        ],
    )
    def test_an_allow_rule(self, tmp_path, rule, path, matches):
        ws = tmp_path.resolve()
        validator = PathValidator(allow_write=[str(ws / rule)])
        assert _allows(validator, ws / path, "write") is matches

    @pytest.mark.parametrize(
        ("rule", "path", "denied"),
        [
            # No wildcard: one literal path, not a file name at any depth.
            ("{ws}/.env", ".env", True),
            ("{ws}/.env", "sub/.env", False),
            ("{ws}/.env", ".env.local", False),
            # A leading ** reaches any depth, and the trailing * any suffix.
            ("**/.env*", ".env", True),
            ("**/.env*", "sub/.env", True),
            ("**/.env*", "sub/.env.local", True),
            # ... which is wider than "secrets files": any name starting
            # .env, and everything under a folder so named.
            ("**/.env*", "src/.envoy.py", True),
            ("**/.env*", ".env/lib/site.py", True),
            ("**/.env*", "src/env.py", False),
            ("**/.env*", "src/my.env", False),
        ],
    )
    def test_a_deny_rule(self, tmp_path, rule, path, denied):
        ws = tmp_path.resolve()
        validator = PathValidator(
            allow_read=[str(ws / "**")], deny=[rule.format(ws=ws)],
        )
        assert _allows(validator, ws / path, "read") is not denied


class TestShippedCodeWriterRules:
    """The ``code-writer`` example in ``config/agents.yaml`` does what the
    comment beside its rules says (ISSUE-0216): writes go under ``src/`` and
    ``tests/`` and to two top-level files named exactly, and a ``.env`` file
    is refused at any depth, with any suffix."""

    @pytest.fixture()
    def rules(self, tmp_path):
        config = yaml.safe_load(
            (_REPO_ROOT / "config" / "agents.yaml").read_text(encoding="utf-8"),
        )
        [agent] = [a for a in config["agents"] if a["id"] == "code-writer"]
        fs = agent["permissions"]["filesystem"]
        ws = tmp_path.resolve()

        def here(patterns: list[str]) -> list[str]:
            """The shipped rules name ``/workspace``; move them to *ws*."""
            return [
                str(ws) + p.removeprefix("/workspace") if p.startswith("/workspace") else p
                for p in patterns
            ]

        validator = PathValidator(
            allow_read=here(fs["read"]), allow_write=here(fs["write"]), deny=here(fs["deny"]),
        )
        return validator, ws

    @pytest.mark.parametrize(
        "path",
        ["deploy/hooks/post_install.py", ".github/workflows/notes.md", "docs/a.md", "malicious.sh"],
    )
    def test_no_write_outside_the_named_folders_and_files(self, rules, path):
        validator, ws = rules
        assert _allows(validator, ws / path, "write") is False

    @pytest.mark.parametrize(
        "path", ["setup.py", "README.md", "src/pkg/mod.py", "tests/unit/test_mod.py"],
    )
    def test_writes_the_comment_promises(self, rules, path):
        validator, ws = rules
        assert _allows(validator, ws / path, "write") is True

    @pytest.mark.parametrize(
        "path",
        [".env", "sub/.env", ".env.local", "src/config/.env.production", ".git/config"],
    )
    @pytest.mark.parametrize("mode", ["read", "write"])
    def test_secrets_files_are_refused_at_any_depth(self, rules, path, mode):
        validator, ws = rules
        with pytest.raises(PermissionError, match="blocked by security policy"):
            validator.validate(str(ws / path), mode=mode)

    def test_the_rest_of_the_workspace_is_readable(self, rules):
        validator, ws = rules
        assert _allows(validator, ws / "docs" / "design.md", "read") is True
        assert _allows(validator, ws / ".github" / "workflows" / "ci.yml", "read") is True


class TestFollowUpFixes:
    """Tests for PR 2 review follow-up findings."""

    def test_fnmatchcase_deterministic_deny(self, tmp_path):
        """S-03: fnmatchcase gives cross-platform deterministic matching."""
        # On Windows, fnmatch.fnmatch is case-insensitive.  We use
        # fnmatchcase so deny patterns behave the same on all OSes.
        target = tmp_path / "SECRET.env"
        target.write_text("sensitive")

        validator = PathValidator(
            allow_read=[str(tmp_path / "**")],
            deny=[str(tmp_path / "*.env")],
        )
        # Lowercase pattern should match lowercase extension
        with pytest.raises(PermissionError, match="blocked by security policy"):
            validator.validate(str(tmp_path / "config.env"), mode="read")

    def test_sanitized_error_no_path_leak(self, tmp_path):
        """S-04: error messages must not include resolved absolute paths."""
        validator = PathValidator(
            allow_read=[str(tmp_path / "src" / "**")],
        )
        with pytest.raises(PermissionError) as exc_info:
            validator.validate(str(tmp_path / "secrets" / "key.pem"), mode="read")
        # Error should NOT contain the resolved absolute path or pattern
        msg = str(exc_info.value)
        assert str(tmp_path) not in msg
        assert "**" not in msg
