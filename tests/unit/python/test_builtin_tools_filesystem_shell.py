"""
Tests for built-in tool implementations.

Validates file_read, file_write, shell_exec, and http_request with
permission enforcement, sandboxing, output truncation, and error handling.
Uses autouse fixture for tool-module state isolation.
"""

import shlex
import sys
from pathlib import Path

import pytest

from agents.tools import builtin
from agents.tools.builtin import MAX_OUTPUT_BYTES
from agents.tools.permissions import PermissionGate
from agents.tools.registry import clear_registry
from agents.tools.sandbox import PathValidator

# ISSUE-0129: the interpreter running these tests, not the bare name
# `python`. `shell_exec` execs through `asyncio.create_subprocess_exec`,
# which does no PATH fallback and no python→python3 aliasing, so a bare
# `python` resolves to nothing on macOS (dropped with the Python 2
# sunset) and the five exec tests failed with `Command not found:
# python` on every Mac while CI stayed green — `actions/setup-python`
# installs a `python` shim.
#
# `_PY` must ALSO be what `_setup_tools` allowlists, absolute path and
# all: `PermissionGate.is_command_allowed` matches by exact token prefix
# (`args[:n] == pattern_parts`), so the entry `"python"` does not admit
# `/…/bin/python3.12`. Fixing the call sites alone just trades
# `FileNotFoundError` for `PermissionError` — see
# `TestIsCommandAllowed.test_denied_absolute_path_for_bare_name_entry`
# in test_permissions.py, which pins that matching rule deliberately.
_PY = sys.executable
_PY_CMD = shlex.quote(_PY)


@pytest.fixture(autouse=True)
def _clean_state():
    """Reset tool registry and module-level state for isolation."""
    clear_registry()
    # Re-import to trigger @tool registration after registry clear
    import importlib

    importlib.reload(builtin)

    # Save originals
    orig_gate = builtin.permission_gate
    orig_validator = builtin.path_validator
    orig_workspace = builtin.workspace_root
    yield
    # Restore originals
    builtin.permission_gate = orig_gate
    builtin.path_validator = orig_validator
    builtin.workspace_root = orig_workspace
    clear_registry()


def _setup_tools(tmp_path: Path, permissions: dict | None = None) -> None:
    """Wire up builtin module-level state for testing."""
    perms = permissions or {
        "filesystem": {
            "read": [str(tmp_path / "**")],
            "write": [str(tmp_path / "**")],
        },
        "shell": {
            "exec": True,
            "allowed_commands": ["echo", _PY, "cat"],
        },
        "network": {
            "allow": ["api.example.com"],
            "deny": ["*"],
            "http": True,
        },
    }
    builtin.permission_gate = PermissionGate(perms)
    builtin.path_validator = PathValidator(
        allow_read=[str(tmp_path / "**")],
        allow_write=[str(tmp_path / "**")],
        deny=[str(tmp_path / ".secret" / "**")],
    )
    builtin.workspace_root = tmp_path


# ─── file_read tests ──────────────────────────────────────


class TestFileRead:
    async def test_read_existing_file(self, tmp_path):
        _setup_tools(tmp_path)
        f = tmp_path / "hello.txt"
        f.write_text("hello world")
        result = await builtin.file_read(str(f))
        assert result.success is True
        assert result.data == "hello world"

    async def test_read_nonexistent_file(self, tmp_path):
        _setup_tools(tmp_path)
        result = await builtin.file_read(str(tmp_path / "missing.txt"))
        assert result.success is False
        assert result.error_type == "FileNotFoundError"

    async def test_read_denied_path(self, tmp_path):
        _setup_tools(tmp_path)
        secret = tmp_path / ".secret" / "key.pem"
        secret.parent.mkdir(parents=True, exist_ok=True)
        secret.write_text("SECRET")
        result = await builtin.file_read(str(secret))
        assert result.success is False
        assert result.error_type == "PermissionError"

    async def test_read_permission_denied(self, tmp_path):
        """No filesystem:read permission → denied."""
        _setup_tools(tmp_path, permissions={"filesystem": {}})
        f = tmp_path / "file.txt"
        f.write_text("data")
        result = await builtin.file_read(str(f))
        assert result.success is False
        assert "Permission denied" in result.error

    async def test_read_large_file_truncated(self, tmp_path):
        _setup_tools(tmp_path)
        f = tmp_path / "big.txt"
        # Write more than MAX_OUTPUT_BYTES
        f.write_text("A" * (MAX_OUTPUT_BYTES + 1000))
        result = await builtin.file_read(str(f))
        assert result.success is True
        assert result.data.endswith("[truncated at 100 KB]")
        assert len(result.data.encode("utf-8")) <= MAX_OUTPUT_BYTES + 100  # overhead

    async def test_read_uninitialized_gate(self, tmp_path):
        builtin.permission_gate = None
        builtin.path_validator = PathValidator(allow_read=[str(tmp_path / "**")])
        # The @tool wrapper catches RuntimeError and wraps it in ToolResult
        result = await builtin.file_read(str(tmp_path / "file.txt"))
        assert result.success is False
        assert result.error_type == "RuntimeError"
        assert "permission_gate is None" in result.error

    async def test_read_binary_file(self, tmp_path):
        """Reading a binary file returns UnicodeDecodeError, not generic OSError."""
        _setup_tools(tmp_path)
        f = tmp_path / "image.png"
        f.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(range(256)))
        result = await builtin.file_read(str(f))
        assert result.success is False
        assert result.error_type == "UnicodeDecodeError"
        assert "Cannot read binary file" in result.error


# ─── file_write tests ──────────────────────────────────────


class TestFileWrite:
    async def test_write_file(self, tmp_path):
        _setup_tools(tmp_path)
        target = tmp_path / "output.txt"
        result = await builtin.file_write(str(target), "hello")
        assert result.success is True
        assert target.read_text() == "hello"
        assert "5 bytes" in result.data

    async def test_write_creates_parent_dirs(self, tmp_path):
        _setup_tools(tmp_path)
        target = tmp_path / "sub" / "dir" / "file.txt"
        result = await builtin.file_write(str(target), "nested")
        assert result.success is True
        assert target.read_text() == "nested"

    async def test_write_denied_path(self, tmp_path):
        _setup_tools(tmp_path)
        secret = tmp_path / ".secret" / "hack.txt"
        result = await builtin.file_write(str(secret), "evil")
        assert result.success is False
        assert result.error_type == "PermissionError"

    async def test_write_permission_denied(self, tmp_path):
        """No filesystem:write permission → denied."""
        _setup_tools(tmp_path, permissions={"filesystem": {}})
        result = await builtin.file_write(str(tmp_path / "file.txt"), "data")
        assert result.success is False
        assert "Permission denied" in result.error

    async def test_write_uninitialized_gate(self, tmp_path):
        """Uninitialized permission_gate → RuntimeError (symmetric with file_read)."""
        builtin.permission_gate = None
        builtin.path_validator = PathValidator(allow_write=[str(tmp_path / "**")])
        result = await builtin.file_write(str(tmp_path / "file.txt"), "data")
        assert result.success is False
        assert result.error_type == "RuntimeError"
        assert "permission_gate is None" in result.error

    async def test_write_multibyte_reports_byte_count(self, tmp_path):
        """Verify byte count is accurate for multi-byte content (emoji, CJK)."""
        _setup_tools(tmp_path)
        target = tmp_path / "emoji.txt"
        content = "hello 🌍"  # 🌍 is 4 UTF-8 bytes
        result = await builtin.file_write(str(target), content)
        assert result.success is True
        expected_bytes = len(content.encode("utf-8"))  # 10, not 7
        assert f"{expected_bytes} bytes" in result.data


# ─── shell_exec tests ──────────────────────────────────────


class TestShellExec:
    async def test_allowed_command(self, tmp_path):
        _setup_tools(tmp_path)
        result = await builtin.shell_exec(f'{_PY_CMD} -c "print(\'hello\')"')
        assert result.success is True
        assert "hello" in result.data["stdout"]
        assert result.data["exit_code"] == 0

    async def test_denied_command(self, tmp_path):
        _setup_tools(tmp_path)
        result = await builtin.shell_exec("rm -rf /")
        assert result.success is False
        assert "not in allowlist" in result.error

    async def test_shell_permission_denied(self, tmp_path):
        """No shell:exec permission → denied."""
        _setup_tools(tmp_path, permissions={"filesystem": {}})
        result = await builtin.shell_exec("echo hi")
        assert result.success is False
        assert "Permission denied" in result.error

    async def test_command_not_found(self, tmp_path):
        _setup_tools(tmp_path, permissions={
            "shell": {
                "exec": True,
                "allowed_commands": ["nonexistent_binary_xyz"],
            },
        })
        result = await builtin.shell_exec("nonexistent_binary_xyz")
        assert result.success is False
        assert result.error_type == "FileNotFoundError"

    async def test_nonzero_exit_code(self, tmp_path):
        _setup_tools(tmp_path)
        result = await builtin.shell_exec(f"{_PY_CMD} -c \"import sys; sys.exit(1)\"")
        assert result.success is False
        assert result.data["exit_code"] == 1

    async def test_timeout_kills_process(self, tmp_path):
        _setup_tools(tmp_path)
        result = await builtin.shell_exec(
            f"{_PY_CMD} -c \"import time; time.sleep(60)\"", timeout=1
        )
        assert result.success is False
        assert result.error_type == "TimeoutError"
        assert "timed out" in result.error

    async def test_empty_command_string(self, tmp_path):
        """Empty string should return a clear error, not IndexError (B-01)."""
        _setup_tools(tmp_path)
        result = await builtin.shell_exec("")
        assert result.success is False
        assert result.error_type == "ValueError"
        assert "Empty command" in result.error

    async def test_invalid_command_syntax(self, tmp_path):
        _setup_tools(tmp_path)
        result = await builtin.shell_exec("echo 'unterminated")
        assert result.success is False
        assert result.error_type == "ValueError"

    async def test_no_shell_true(self):
        """Verify no shell=True usage in builtin.py source."""
        import inspect

        source = inspect.getsource(builtin)
        assert "shell=True" not in source

    async def test_large_stdout_truncated(self, tmp_path):
        """Verify MAX_OUTPUT_BYTES truncation on large shell output."""
        _setup_tools(tmp_path)
        # Generate output exceeding MAX_OUTPUT_BYTES (100 KB)
        result = await builtin.shell_exec(
            f'{_PY_CMD} -c "print(\'A\' * 200000)"'
        )
        assert result.success is True
        assert result.data["stdout"].endswith("[truncated at 100 KB]")

    async def test_timeout_clamped_to_bounds(self, tmp_path):
        """Verify timeout is clamped to [1, MAX_TIMEOUT_SECONDS]."""
        _setup_tools(tmp_path)
        # timeout=0 should be clamped to 1, not cause immediate failure
        result = await builtin.shell_exec(f'{_PY_CMD} -c "print(\'ok\')"', timeout=0)
        assert result.success is True
        assert "ok" in result.data["stdout"]


# ─── Output truncation ──────────────────────────────────────


class TestTruncation:
    def test_short_text_unchanged(self):
        from agents.tools.builtin import _truncate

        assert _truncate("short") == "short"

    def test_long_text_truncated(self):
        from agents.tools.builtin import _truncate

        long_text = "X" * (MAX_OUTPUT_BYTES + 500)
        result = _truncate(long_text)
        assert result.endswith("[truncated at 100 KB]")
        assert len(result) < len(long_text)
