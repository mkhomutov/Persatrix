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
    ChannelsDisabledError,
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

    async def test_channel_id_is_url_encoded(self):
        """``channel_id`` is LLM-supplied and MUST be percent-encoded into the URL.

        PR #250 review (Must-Fix #1, OWASP A03): ``channel_id`` flows from
        the LLM action payload (``action.payload["channel_id"]``) directly
        into the publish URL.  An unencoded ``../admin`` would resolve to
        an unintended path on the orchestrator side; ``?`` would smuggle
        a query string; ``#`` would be silently truncated client-side
        (fragments are not sent), losing data without an error.  The
        orchestrator's ``validateChannelID`` does reject such IDs once the
        request lands, but the malformed path *gets sent* and is recorded
        in access logs and metrics — defense in depth says encode at the
        boundary.

        We assert against the URL the publisher hands to ``session.post``
        rather than against an aiohttp loopback server because aiohttp's
        path-routing decodes ``%2F`` back to ``/``, which would split the
        ``{channel_id}`` segment in the route and mask a regression.
        """
        from unittest.mock import AsyncMock, MagicMock

        # Minimal session double: ``post`` is an async ctx mgr returning
        # a response with status<400 so ``publish`` returns cleanly and
        # we can inspect the URL it built.
        resp = MagicMock()
        resp.status = 201
        resp.text = AsyncMock(return_value="")
        # ISSUE-0032: the publisher reads the response body to capture
        # the orchestrator-assigned ``message_id`` for the OTel span;
        # the mock must satisfy that read even though this test only
        # cares about the URL the publisher built.
        resp.json = AsyncMock(return_value={"id": "ignored-by-this-test"})

        post_ctx = MagicMock()
        post_ctx.__aenter__ = AsyncMock(return_value=resp)
        post_ctx.__aexit__ = AsyncMock(return_value=None)

        session = MagicMock()
        session.post = MagicMock(return_value=post_ctx)

        pub = HTTPChannelPublisher(
            orchestrator_url="http://orch.example",
            session=session,  # type: ignore[arg-type]
        )
        # Every char below is URL-reserved; a raw f-string interpolation
        # would let ``/`` add path components, ``?`` start a query, and
        # ``#`` silently drop the rest of the URL on the wire.
        await pub.publish(
            channel_id="../admin?x=1#frag space",
            sender_id="agent-a",
            content="hi",
            mentions=[],
        )
        url = session.post.call_args.args[0]
        assert url == (
            "http://orch.example/api/v1/channels/"
            "..%2Fadmin%3Fx%3D1%23frag%20space"
            "/messages"
        ), (
            "channel_id must be percent-encoded with safe='' so reserved "
            f"characters cannot escape the path segment; got url={url!r}"
        )


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


class TestStickyDisabledOnHTTP503:
    """ISSUE-0026 — `HTTPChannelPublisher` short-circuits after first 503.

    Same agent build is intended to run against orchestrators with and
    without channels enabled (per the deferred-by-default phase model in
    `cmd/orchestrator/channels.go::selectChannelDispatcher`). Without the
    sticky-disabled flag every `SEND_CHANNEL_MESSAGE` action against a
    channels-disabled orchestrator hits 503, drowning the operator log in
    one WARN per action and burning an HTTP RTT each time.

    The contract pinned here:

    * First 503 from `POST /api/v1/channels/{id}/messages` flips a sticky
      `_disabled` flag, emits a one-shot WARN with the response body for
      diagnostics, and raises a typed `ChannelsDisabledError` so callers
      can map it to a distinct status.
    * Subsequent `publish()` calls raise `ChannelsDisabledError`
      immediately without any HTTP roundtrip — the loopback handler's
      hit counter must NOT increment.
    * The one-shot WARN fires exactly once even across many short-circuit
      calls.
    * Other 4xx/5xx statuses (403, 404, 500) do NOT flip the flag — those
      are per-message conditions, not deployment-wide signals.
    """

    @pytest.fixture
    async def disabled_server(self):
        """Loopback server that always returns 503 and counts hits."""
        hits: list[str] = []

        async def handler(request: web.Request) -> web.Response:
            hits.append(request.path)
            return web.json_response(
                {"error": "channels disabled"}, status=503,
            )

        app = web.Application()
        app.router.add_post("/api/v1/channels/{id}/messages", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        try:
            yield f"http://127.0.0.1:{port}", hits
        finally:
            await runner.cleanup()

    async def test_first_503_raises_channels_disabled_and_flips_flag(
        self, disabled_server, caplog,
    ):
        base_url, hits = disabled_server
        async with aiohttp.ClientSession() as session:
            pub = HTTPChannelPublisher(orchestrator_url=base_url, session=session)
            with caplog.at_level("WARNING", logger="agents.channel_publisher"):
                with pytest.raises(ChannelsDisabledError):
                    await pub.publish(
                        channel_id="group:planning",
                        sender_id="agent-a",
                        content="hi",
                        mentions=[],
                    )
        # First publish reached the wire (one hit).
        assert len(hits) == 1
        # Flag is now sticky.
        assert pub._disabled is True
        # One-shot WARN fired with diagnostic body.
        warn_records = [
            r for r in caplog.records
            if r.levelname == "WARNING" and "503" in r.getMessage()
        ]
        assert len(warn_records) == 1, (
            f"expected exactly one 503 WARN, got {len(warn_records)}: "
            f"{[r.getMessage() for r in warn_records]}"
        )

    async def test_subsequent_publish_short_circuits_without_http(
        self, disabled_server,
    ):
        base_url, hits = disabled_server
        async with aiohttp.ClientSession() as session:
            pub = HTTPChannelPublisher(orchestrator_url=base_url, session=session)
            # Trip the sticky flag.
            with pytest.raises(ChannelsDisabledError):
                await pub.publish(
                    channel_id="group:planning",
                    sender_id="agent-a",
                    content="hi",
                    mentions=[],
                )
            assert len(hits) == 1
            # Three more attempts must NOT hit the wire.
            for _ in range(3):
                with pytest.raises(ChannelsDisabledError):
                    await pub.publish(
                        channel_id="group:planning",
                        sender_id="agent-a",
                        content="again",
                        mentions=[],
                    )
        assert len(hits) == 1, (
            f"expected zero further HTTP hits after sticky-disable, "
            f"got {len(hits) - 1}"
        )

    async def test_one_shot_warn_does_not_repeat(
        self, disabled_server, caplog,
    ):
        base_url, _ = disabled_server
        async with aiohttp.ClientSession() as session:
            pub = HTTPChannelPublisher(orchestrator_url=base_url, session=session)
            with caplog.at_level("WARNING", logger="agents.channel_publisher"):
                for _ in range(5):
                    with pytest.raises(ChannelsDisabledError):
                        await pub.publish(
                            channel_id="group:planning",
                            sender_id="agent-a",
                            content="hi",
                            mentions=[],
                        )
        warn_records = [
            r for r in caplog.records
            if r.levelname == "WARNING" and "503" in r.getMessage()
        ]
        assert len(warn_records) == 1, (
            "the disabled WARN must fire only on the first 503, not on "
            f"every short-circuit; got {len(warn_records)} WARNs"
        )

    async def test_403_does_not_flip_disabled_flag(self):
        """Per-message 403 is not a deployment-wide signal."""
        async def handler_403(request: web.Request) -> web.Response:
            await request.read()
            return web.json_response({"error": "NOT_MEMBER"}, status=403)

        app = web.Application()
        app.router.add_post("/api/v1/channels/{id}/messages", handler_403)
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
                )
                with pytest.raises(aiohttp.ClientResponseError):
                    await pub.publish(
                        channel_id="group:planning",
                        sender_id="agent-a",
                        content="hi",
                        mentions=[],
                    )
                # 403 is per-message; the publisher must remain enabled so
                # other channels keep working.
                assert pub._disabled is False
        finally:
            await runner.cleanup()

    async def test_500_does_not_flip_disabled_flag(self):
        """Generic 5xx is treated as transient; only 503 is the disabled signal."""
        async def handler_500(request: web.Request) -> web.Response:
            await request.read()
            return web.json_response({"error": "internal"}, status=500)

        app = web.Application()
        app.router.add_post("/api/v1/channels/{id}/messages", handler_500)
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
                )
                with pytest.raises(aiohttp.ClientResponseError):
                    await pub.publish(
                        channel_id="group:planning",
                        sender_id="agent-a",
                        content="hi",
                        mentions=[],
                    )
                assert pub._disabled is False
        finally:
            await runner.cleanup()
