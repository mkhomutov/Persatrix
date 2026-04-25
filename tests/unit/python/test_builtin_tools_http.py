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
