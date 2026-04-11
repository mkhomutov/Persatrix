"""
Tests for PathValidator.

Validates workspace-scoped path restriction with deny-wins semantics,
symlink resolution, and mode-based access control.
"""

import os
import platform

import pytest

from agents.tools.sandbox import PathValidator


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
