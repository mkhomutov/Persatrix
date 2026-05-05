"""Tests for :class:`agents.channel_publisher.HTTPChannelPublisher`.

Uses :mod:`aiohttp.test_utils` to spin up a real loopback HTTP server so
the publisher exercises the real session, JSON marshaling, and status
handling rather than mocking ``aiohttp.ClientSession.post``.
"""

from __future__ import annotations

import asyncio

import aiohttp
import pytest
from aiohttp import web

from agents.channel_publisher import (
    DEFAULT_PUBLISH_TIMEOUT_SECONDS,
    HTTPChannelPublisher,
)


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


class TestPublishTimeout:
    """Regression tests for PR #250 review (Should-Fix #1).

    The previous implementation hard-coded ``aiohttp.ClientTimeout(total=10)``
    inside :meth:`HTTPChannelPublisher.publish` *and* the executor wrapped
    the same call in :func:`asyncio.wait_for` with an independent ``10`` —
    two timers tuned to the same value but maintained in two places. The
    fix introduces :data:`DEFAULT_PUBLISH_TIMEOUT_SECONDS` as the single
    source of truth and parameterizes the publisher's timeout, so both
    layers stay in sync from one constant.
    """

    def test_default_timeout_constant_exposed(self):
        # Single-source-of-truth: the executor must be able to import the
        # same constant rather than redefining its own.
        assert isinstance(DEFAULT_PUBLISH_TIMEOUT_SECONDS, float)
        assert DEFAULT_PUBLISH_TIMEOUT_SECONDS > 0

    def test_executor_constant_is_published_constant(self):
        # The executor's _DEFAULT_PUBLISH_HTTP_TIMEOUT must equal the
        # publisher's exported constant — if they ever drift, callers face
        # the same two-timer race the original review flagged.
        from agents import action_executor

        assert (
            action_executor._DEFAULT_PUBLISH_HTTP_TIMEOUT
            == DEFAULT_PUBLISH_TIMEOUT_SECONDS
        )

    async def test_publisher_accepts_custom_timeout(self, captured_server):
        # The publisher exposes a `timeout` constructor arg so an operator
        # can raise it (slow orchestrator) or lower it (tight SLO) without
        # patching the module-level constant.
        base_url, _ = captured_server
        async with aiohttp.ClientSession() as session:
            pub = HTTPChannelPublisher(
                orchestrator_url=base_url, session=session, timeout=2.5,
            )
            # Internal field — exposed for this regression test only; the
            # public contract is "timeout flows through to aiohttp".
            assert pub._timeout == 2.5

    async def test_publisher_short_timeout_raises_on_slow_server(self):
        """Behavioral check that the constructor timeout reaches aiohttp.

        A 50 ms timeout against a server that holds the response for
        500 ms must raise an aiohttp/asyncio timeout — proving the value
        is honored on the wire and not just stored on the instance.
        """
        async def slow_handler(request: web.Request) -> web.Response:
            await asyncio.sleep(0.5)
            return web.json_response({"id": "m"}, status=201)

        app = web.Application()
        app.router.add_post("/api/v1/channels/{id}/messages", slow_handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        try:
            async with aiohttp.ClientSession() as session:
                pub = HTTPChannelPublisher(
                    orchestrator_url=f"http://127.0.0.1:{port}",
                    session=session,
                    timeout=0.05,
                )
                # aiohttp raises asyncio.TimeoutError (or a subclass) on
                # ClientTimeout exhaustion; the executor's broad except
                # turns either into status="failed".
                with pytest.raises((asyncio.TimeoutError, aiohttp.ServerTimeoutError)):
                    await pub.publish(
                        channel_id="group:planning",
                        sender_id="agent-a",
                        content="hi",
                        mentions=[],
                    )
        finally:
            await runner.cleanup()
