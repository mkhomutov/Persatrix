"""
Tests for built-in tool implementations.

Validates file_read, file_write, shell_exec, and http_request with
permission enforcement, sandboxing, output truncation, and error handling.
Uses autouse fixture for tool-module state isolation.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
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

    async def test_large_stdout_truncated(self, tmp_path):
        """Verify MAX_OUTPUT_BYTES truncation on large shell output."""
        _setup_tools(tmp_path)
        # Generate output exceeding MAX_OUTPUT_BYTES (100 KB)
        result = await builtin.shell_exec(
            'python -c "print(\'A\' * 200000)"'
        )
        assert result.success is True
        assert result.data["stdout"].endswith("[truncated at 100 KB]")

    async def test_timeout_clamped_to_bounds(self, tmp_path):
        """Verify timeout is clamped to [1, MAX_TIMEOUT_SECONDS]."""
        _setup_tools(tmp_path)
        # timeout=0 should be clamped to 1, not cause immediate failure
        result = await builtin.shell_exec('python -c "print(\'ok\')"', timeout=0)
        assert result.success is True
        assert "ok" in result.data["stdout"]


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
        # Content-Type defaults to application/json for body requests (S-01)
        assert call_kwargs[1]["headers"] == {"Content-Type": "application/json"}
        # Redirects disabled to prevent SSRF (M-01)
        assert call_kwargs[1]["allow_redirects"] is False

    async def test_get_does_not_forward_body(self, tmp_path):
        """Verify body is NOT forwarded for GET requests."""
        _setup_tools(tmp_path)
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.text = AsyncMock(return_value="ok")
        mock_resp.headers = {}
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.request = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("agents.tools.builtin.aiohttp.ClientSession", return_value=mock_session):
            result = await builtin.http_request(
                "https://api.example.com/v1/data", method="GET", body="ignored"
            )

        assert result.success is True
        call_kwargs = mock_session.request.call_args[1]
        assert "data" not in call_kwargs

    async def test_delete_method_rejected(self, tmp_path):
        """DELETE method is not in ALLOWED_HTTP_METHODS (prevents data loss)."""
        _setup_tools(tmp_path)
        result = await builtin.http_request(
            "https://api.example.com/v1/resource/123", method="DELETE"
        )
        assert result.success is False
        assert "not allowed" in result.error
        assert result.error_type == "ValueError"

    async def test_client_connection_error(self, tmp_path):
        """aiohttp.ClientError is caught and returned as ToolResult."""
        _setup_tools(tmp_path)
        with patch(
            "agents.tools.builtin.aiohttp.ClientSession",
            side_effect=aiohttp.ClientConnectionError("Connection refused"),
        ):
            result = await builtin.http_request("https://api.example.com/v1/data")

        assert result.success is False
        assert result.error_type == "ClientConnectionError"

    async def test_http_timeout_error(self, tmp_path):
        """HTTP timeout is caught and returned as ToolResult."""
        _setup_tools(tmp_path)

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.request = MagicMock(side_effect=TimeoutError("Request timed out"))

        with patch("agents.tools.builtin.aiohttp.ClientSession", return_value=mock_session):
            result = await builtin.http_request("https://api.example.com/v1/slow")

        assert result.success is False
        assert result.error_type == "TimeoutError"
        assert "timed out" in result.error

    async def test_large_response_truncated(self, tmp_path):
        """Verify large HTTP response body is truncated via _truncate()."""
        _setup_tools(tmp_path)
        large_body = "B" * (MAX_OUTPUT_BYTES + 5000)

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.text = AsyncMock(return_value=large_body)
        mock_resp.headers = {"Content-Type": "text/plain"}
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.request = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("agents.tools.builtin.aiohttp.ClientSession", return_value=mock_session):
            result = await builtin.http_request("https://api.example.com/v1/large")

        assert result.success is True
        assert result.data["body"].endswith("[truncated at 100 KB]")

    async def test_redirect_not_followed(self, tmp_path):
        """Redirects are not followed to prevent SSRF via open-redirect on
        allowlisted domains (M-01). The raw 3xx response is returned so the
        LLM can re-issue a request to the redirect target after domain
        re-validation."""
        _setup_tools(tmp_path)
        mock_resp = AsyncMock()
        mock_resp.status = 302
        mock_resp.text = AsyncMock(return_value="")
        mock_resp.headers = {"Location": "https://evil.internal/metadata"}
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.request = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("agents.tools.builtin.aiohttp.ClientSession", return_value=mock_session):
            result = await builtin.http_request("https://api.example.com/redirect")

        assert result.success is True
        assert result.data["status"] == 302
        # Verify allow_redirects=False was passed to aiohttp
        call_kwargs = mock_session.request.call_args[1]
        assert call_kwargs["allow_redirects"] is False

    async def test_put_with_body(self, tmp_path):
        """Verify PUT method is accepted and body + Content-Type forwarded (S-04)."""
        _setup_tools(tmp_path)
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.text = AsyncMock(return_value='{"updated": true}')
        mock_resp.headers = {"Content-Type": "application/json"}
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.request = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("agents.tools.builtin.aiohttp.ClientSession", return_value=mock_session):
            result = await builtin.http_request(
                "https://api.example.com/v1/items/1", method="PUT", body='{"name": "updated"}'
            )

        assert result.success is True
        call_kwargs = mock_session.request.call_args[1]
        assert call_kwargs["method"] == "PUT"
        assert call_kwargs["data"] == '{"name": "updated"}'
        assert call_kwargs["headers"] == {"Content-Type": "application/json"}

    async def test_patch_with_body(self, tmp_path):
        """Verify PATCH method is accepted and body + Content-Type forwarded (S-04)."""
        _setup_tools(tmp_path)
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.text = AsyncMock(return_value='{"patched": true}')
        mock_resp.headers = {"Content-Type": "application/json"}
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.request = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("agents.tools.builtin.aiohttp.ClientSession", return_value=mock_session):
            result = await builtin.http_request(
                "https://api.example.com/v1/items/1", method="PATCH", body='{"status": "done"}'
            )

        assert result.success is True
        call_kwargs = mock_session.request.call_args[1]
        assert call_kwargs["method"] == "PATCH"
        assert call_kwargs["data"] == '{"status": "done"}'
        assert call_kwargs["headers"] == {"Content-Type": "application/json"}


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

    def test_multibyte_boundary_no_replacement_chars(self):
        """S-07: Truncation at multi-byte character boundary must not produce
        \\ufffd replacement characters. Uses errors='ignore' to drop incomplete
        sequences instead."""
        from agents.tools.builtin import _truncate

        # Fill up to just below MAX_OUTPUT_BYTES with ASCII, then add emoji
        # so the boundary falls in the middle of a multi-byte character.
        padding = "A" * (MAX_OUTPUT_BYTES - 2)
        emoji_suffix = "🌍🌍🌍"  # Each emoji is 4 UTF-8 bytes
        text = padding + emoji_suffix
        result = _truncate(text)
        # No replacement characters should appear in the output
        assert "\ufffd" not in result
        assert result.endswith("[truncated at 100 KB]")

    def test_cjk_boundary_no_replacement_chars(self):
        """S-07: CJK characters (3-byte UTF-8) at boundary also safe."""
        from agents.tools.builtin import _truncate

        # CJK characters are 3 bytes each in UTF-8
        padding = "A" * (MAX_OUTPUT_BYTES - 1)
        cjk_suffix = "中文字符" * 10
        text = padding + cjk_suffix
        result = _truncate(text)
        assert "\ufffd" not in result


# ─── Response header filtering ──────────────────────────────


class TestResponseHeaderFiltering:
    async def test_safe_headers_returned(self, tmp_path):
        """S-06: Only safe headers are returned to the LLM."""
        _setup_tools(tmp_path)
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.text = AsyncMock(return_value="ok")
        mock_resp.headers = {
            "Content-Type": "application/json",
            "Content-Length": "2",
            "Date": "Fri, 11 Apr 2026 00:00:00 GMT",
            "Set-Cookie": "session=abc123; HttpOnly",
            "Server": "nginx/1.25.0",
            "X-Powered-By": "Express",
            "X-Request-Id": "req-12345",
        }
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.request = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("agents.tools.builtin.aiohttp.ClientSession", return_value=mock_session):
            result = await builtin.http_request("https://api.example.com/v1/data")

        headers = result.data["headers"]
        # Safe headers present
        assert "Content-Type" in headers
        assert "Content-Length" in headers
        assert "Date" in headers
        # Sensitive headers filtered out
        assert "Set-Cookie" not in headers
        assert "Server" not in headers
        assert "X-Powered-By" not in headers
        assert "X-Request-Id" not in headers
