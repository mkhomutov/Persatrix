"""
Tests for built-in tool implementations.

Validates file_read, file_write, shell_exec, and http_request with
permission enforcement, sandboxing, output truncation, and error handling.
Uses autouse fixture for tool-module state isolation.
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.tools import builtin
from agents.tools.builtin import MAX_OUTPUT_BYTES
from agents.tools.permissions import PermissionGate
from agents.tools.registry import clear_registry
from agents.tools.sandbox import PathValidator


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
            "allowed_commands": ["echo", "python", "cat"],
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


# ─── shell_exec tests ──────────────────────────────────────


class TestShellExec:
    async def test_allowed_command(self, tmp_path):
        _setup_tools(tmp_path)
        result = await builtin.shell_exec('python -c "print(\'hello\')"')
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
        result = await builtin.shell_exec("python -c \"import sys; sys.exit(1)\"")
        assert result.success is False
        assert result.data["exit_code"] == 1

    async def test_timeout_kills_process(self, tmp_path):
        _setup_tools(tmp_path)
        result = await builtin.shell_exec(
            "python -c \"import time; time.sleep(60)\"", timeout=1
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


# ─── http_request tests ──────────────────────────────────────


class TestHttpRequest:
    async def test_allowed_domain(self, tmp_path):
        _setup_tools(tmp_path)
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.text = AsyncMock(return_value='{"ok": true}')
        mock_resp.headers = {"Content-Type": "application/json"}
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.request = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("agents.tools.builtin.aiohttp.ClientSession", return_value=mock_session):
            result = await builtin.http_request("https://api.example.com/v1/chat")

        assert result.success is True
        assert result.data["status"] == 200
        assert result.data["body"] == '{"ok": true}'

    async def test_denied_domain(self, tmp_path):
        _setup_tools(tmp_path)
        result = await builtin.http_request("https://evil.com/hack")
        assert result.success is False
        assert "not in allowlist" in result.error

    async def test_file_scheme_rejected(self, tmp_path):
        _setup_tools(tmp_path)
        result = await builtin.http_request("file:///etc/passwd")
        assert result.success is False
        assert "Unsupported URL scheme" in result.error

    async def test_ftp_scheme_rejected(self, tmp_path):
        _setup_tools(tmp_path)
        result = await builtin.http_request("ftp://files.example.com/data")
        assert result.success is False
        assert "Unsupported URL scheme" in result.error

    async def test_network_permission_denied(self, tmp_path):
        """No network:http permission → denied."""
        _setup_tools(tmp_path, permissions={"filesystem": {}})
        result = await builtin.http_request("https://api.example.com")
        assert result.success is False
        assert "Permission denied" in result.error

    async def test_malformed_url(self, tmp_path):
        _setup_tools(tmp_path)
        result = await builtin.http_request("not-a-url")
        assert result.success is False

    async def test_empty_hostname(self, tmp_path):
        _setup_tools(tmp_path)
        result = await builtin.http_request("https://")
        assert result.success is False
        assert "no hostname" in result.error

    async def test_post_with_body(self, tmp_path):
        """Verify body is forwarded for POST requests (S-03 coverage)."""
        _setup_tools(tmp_path)
        mock_resp = AsyncMock()
        mock_resp.status = 201
        mock_resp.text = AsyncMock(return_value='{"created": true}')
        mock_resp.headers = {"Content-Type": "application/json"}
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.request = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("agents.tools.builtin.aiohttp.ClientSession", return_value=mock_session):
            result = await builtin.http_request(
                "https://api.example.com/v1/items", method="POST", body='{"name": "test"}'
            )

        assert result.success is True
        assert result.data["status"] == 201
        # Verify body was passed as data kwarg
        call_kwargs = mock_session.request.call_args
        assert call_kwargs[1]["data"] == '{"name": "test"}'
        assert call_kwargs[1]["method"] == "POST"


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
