"""Tests for :class:`agents.channel_publisher.HTTPChannelPublisher`.

Uses :mod:`aiohttp.test_utils` to spin up a real loopback HTTP server so
the publisher exercises the real session, JSON marshaling, and status
handling rather than mocking ``aiohttp.ClientSession.post``.
"""

from __future__ import annotations

import aiohttp
import pytest
from aiohttp import web

from agents.channel_publisher import HTTPChannelPublisher


@pytest.fixture
async def captured_server():
    """Start a loopback aiohttp server that records every POST body."""
    captured: list[dict] = []

    async def handler(request: web.Request) -> web.Response:
        body = await request.json()
        captured.append({"path": request.path, "body": body})
        return web.json_response({"id": "m-1"}, status=201)

    async def error_handler(request: web.Request) -> web.Response:
        await request.read()
        return web.json_response({"error": "NOT_MEMBER"}, status=403)

    app = web.Application()
    app.router.add_post("/api/v1/channels/{id}/messages", handler)
    app.router.add_post("/api/v1/channels/forbidden/messages", error_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    try:
        yield f"http://127.0.0.1:{port}", captured
    finally:
        await runner.cleanup()


class TestHTTPChannelPublisher:

    async def test_happy_path_posts_to_correct_url(self, captured_server):
        base_url, captured = captured_server
        async with aiohttp.ClientSession() as session:
            pub = HTTPChannelPublisher(orchestrator_url=base_url, session=session)
            await pub.publish(
                channel_id="group:planning",
                sender_id="agent-a",
                content="hi",
                mentions=["agent-b"],
            )
        assert len(captured) == 1
        assert captured[0]["path"] == "/api/v1/channels/group:planning/messages"
        assert captured[0]["body"] == {
            "sender_id": "agent-a",
            "content": "hi",
            "mentions": ["agent-b"],
        }

    async def test_empty_mentions_omitted_from_body(self, captured_server):
        base_url, captured = captured_server
        async with aiohttp.ClientSession() as session:
            pub = HTTPChannelPublisher(orchestrator_url=base_url, session=session)
            await pub.publish(
                channel_id="group:planning",
                sender_id="agent-a",
                content="hi",
                mentions=[],
            )
        assert "mentions" not in captured[0]["body"]

    async def test_non_2xx_raises(self, captured_server):
        base_url, _ = captured_server
        async with aiohttp.ClientSession() as session:
            pub = HTTPChannelPublisher(orchestrator_url=base_url, session=session)
            with pytest.raises(aiohttp.ClientResponseError):
                await pub.publish(
                    channel_id="forbidden",
                    sender_id="agent-a",
                    content="hi",
                    mentions=[],
                )

    async def test_empty_channel_id_raises_value_error(self):
        # Defensive guard: never POST to /api/v1/channels//messages.
        async with aiohttp.ClientSession() as session:
            pub = HTTPChannelPublisher(orchestrator_url="http://x", session=session)
            with pytest.raises(ValueError, match="channel_id"):
                await pub.publish(
                    channel_id="",
                    sender_id="agent-a",
                    content="hi",
                    mentions=[],
                )

    async def test_orchestrator_url_trailing_slash_normalized(self, captured_server):
        base_url, captured = captured_server
        async with aiohttp.ClientSession() as session:
            pub = HTTPChannelPublisher(
                orchestrator_url=base_url + "/", session=session,
            )
            await pub.publish(
                channel_id="group:planning",
                sender_id="agent-a",
                content="hi",
                mentions=[],
            )
        # No double-slash before /api.
        assert captured[0]["path"] == "/api/v1/channels/group:planning/messages"
