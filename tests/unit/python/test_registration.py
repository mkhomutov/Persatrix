"""
Tests for agent self-registration and de-registration with orchestrator.

All tests use mock HTTP — no real network calls.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp

from agents.base import BaseAgent, TaskInput, TaskOutput, TaskStatus
from agents.server import AgentServer

# ─── Helpers ─────────────────────────────────────────────────


class _StubAgent(BaseAgent):
    """Minimal agent for registration tests."""

    async def handle(self, task: TaskInput) -> TaskOutput:
        return TaskOutput(
            status=TaskStatus.COMPLETED,
            result="stub result",
        )


def _make_server(orchestrator_url: str = "http://127.0.0.1:8080") -> AgentServer:
    server = AgentServer(
        host="127.0.0.1",
        port=0,
        shutdown_grace=1,
        orchestrator_url=orchestrator_url,
        advertise_address="127.0.0.1:50051",
    )
    agent = _StubAgent(
        agent_id="test-agent",
        config={"capabilities": ["planning"]},
    )
    server.register_agent(agent)
    return server


# ─── Self-Registration Tests ────────────────────────────────


class TestSelfRegistration:
    """Tests for AgentServer._self_register()."""

    async def test_successful_registration(self):
        """Registration sends correct payload and logs on 201."""
        server = _make_server()
        mock_resp = AsyncMock()
        mock_resp.status = 201
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock(spec=aiohttp.ClientSession)
        mock_session.post = MagicMock(return_value=mock_resp)
        server._session = mock_session
        server.port = 50051

        await server._self_register()

        mock_session.post.assert_called_once()
        call_kwargs = mock_session.post.call_args
        assert call_kwargs[0][0] == "http://127.0.0.1:8080/api/v1/agents/register"
        payload = call_kwargs[1]["json"]
        assert payload["id"] == "test-agent"
        assert payload["name"] == "test-agent"  # S-19: name included in payload
        assert payload["address"] == "127.0.0.1:50051"
        assert payload["capabilities"] == ["planning"]
        # P1: status is NOT sent in payload
        assert "status" not in payload

    async def test_successful_registration_200(self):
        """Registration succeeds on HTTP 200 (not just 201)."""
        server = _make_server()
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock(spec=aiohttp.ClientSession)
        mock_session.post = MagicMock(return_value=mock_resp)
        server._session = mock_session
        server.port = 50051

        # S-01: verify success branch logs INFO, not just that method doesn't crash
        with patch("agents.server.logger") as mock_logger:
            await server._self_register()

        mock_session.post.assert_called_once()
        mock_logger.info.assert_any_call(
            "Registered agent %s with orchestrator at %s",
            "test-agent",
            "http://127.0.0.1:8080",
        )

    async def test_registration_conflict_409(self):
        """Agent already registered (409) is handled gracefully."""
        server = _make_server()
        mock_resp = AsyncMock()
        mock_resp.status = 409
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock(spec=aiohttp.ClientSession)
        mock_session.post = MagicMock(return_value=mock_resp)
        server._session = mock_session
        server.port = 50051

        # Should not raise
        await server._self_register()

    async def test_registration_server_error(self):
        """Non-success status code is logged as warning but does not raise."""
        server = _make_server()
        mock_resp = AsyncMock()
        mock_resp.status = 500
        mock_resp.text = AsyncMock(return_value="Internal Server Error")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock(spec=aiohttp.ClientSession)
        mock_session.post = MagicMock(return_value=mock_resp)
        server._session = mock_session
        server.port = 50051

        # Should not raise
        await server._self_register()

    async def test_registration_orchestrator_unreachable(self):
        """Connection error is logged as warning, server continues."""
        server = _make_server()
        mock_session = AsyncMock(spec=aiohttp.ClientSession)
        mock_session.post = MagicMock(
            side_effect=aiohttp.ClientError("connection refused")
        )
        server._session = mock_session
        server.port = 50051

        # Should not raise — best effort
        await server._self_register()

    async def test_registration_no_session(self):
        """No-op when session is None (server not started)."""
        server = _make_server()
        server._session = None
        # Should not raise
        await server._self_register()


# ─── De-Registration Tests ──────────────────────────────────


class TestSelfDeregistration:
    """Tests for AgentServer._self_deregister()."""

    async def test_successful_deregistration(self):
        """De-registration calls DELETE /api/v1/agents/{id}."""
        server = _make_server()
        mock_resp = AsyncMock()
        mock_resp.status = 204
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock(spec=aiohttp.ClientSession)
        mock_session.delete = MagicMock(return_value=mock_resp)
        server._session = mock_session

        await server._self_deregister()

        mock_session.delete.assert_called_once()
        call_args = mock_session.delete.call_args
        assert call_args[0][0] == "http://127.0.0.1:8080/api/v1/agents/test-agent"

    async def test_deregistration_failure_is_warning(self):
        """5xx from orchestrator during de-registration does not raise."""
        server = _make_server()
        mock_resp = AsyncMock()
        mock_resp.status = 500
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock(spec=aiohttp.ClientSession)
        mock_session.delete = MagicMock(return_value=mock_resp)
        server._session = mock_session

        # Should not raise (PR-review B5: best-effort)
        await server._self_deregister()

    async def test_deregistration_orchestrator_down(self):
        """Connection error during de-registration is logged, not raised."""
        server = _make_server()
        mock_session = AsyncMock(spec=aiohttp.ClientSession)
        mock_session.delete = MagicMock(
            side_effect=aiohttp.ClientError("connection refused")
        )
        server._session = mock_session

        # Should not raise
        await server._self_deregister()

    async def test_deregistration_no_session(self):
        """No-op when session is None."""
        server = _make_server()
        server._session = None
        # Should not raise
        await server._self_deregister()


# ─── Session Lifecycle Tests ────────────────────────────────


class TestSessionLifecycle:
    """Tests for shared aiohttp.ClientSession lifecycle."""

    async def test_session_created_on_start(self):
        """start() creates an aiohttp.ClientSession."""
        server = _make_server()
        assert server._session is None

        with patch.object(server, "_self_register", new_callable=AsyncMock):
            await server.start()
            assert server._session is not None
            assert isinstance(server._session, aiohttp.ClientSession)
            await server.stop()

    async def test_session_closed_on_stop(self):
        """stop() closes the aiohttp.ClientSession."""
        server = _make_server()
        with patch.object(server, "_self_register", new_callable=AsyncMock):
            with patch.object(server, "_self_deregister", new_callable=AsyncMock):
                await server.start()
                session = server._session
                await server.stop()
                assert server._session is None
                assert session.closed

    async def test_stop_calls_deregister_before_grpc_stop(self):
        """stop() de-registers agents before stopping gRPC server."""
        server = _make_server()
        call_order: list[str] = []

        async def track_deregister():
            call_order.append("deregister")

        with patch.object(server, "_self_register", new_callable=AsyncMock):
            await server.start()

        server._self_deregister = track_deregister  # type: ignore[assignment]
        original_stop = server._server.stop

        async def track_grpc_stop(grace=0):
            call_order.append("grpc_stop")
            await original_stop(grace=grace)

        server._server.stop = track_grpc_stop  # type: ignore[assignment]

        await server.stop()

        assert call_order == ["deregister", "grpc_stop"]
