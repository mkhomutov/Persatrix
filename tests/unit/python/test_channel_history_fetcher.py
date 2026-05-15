"""RFC 0034 Phase 1 PR 1 — channel-history fetcher behind a Protocol.

Pins the contract of :mod:`agents.channel_history_fetcher`, the module
factored out of ``agents.channel_catchup._fetch_channel_history`` so
the on-startup catch-up replay and the RFC 0034 conversation-window
substrate (PR 2) can share one history-fetch seam.

Contract under test:

* :meth:`HttpChannelHistoryFetcher.fetch` returns the server's
  ``messages`` array on success and ``[]`` when that field is absent
  or not a list.
* Any HTTP 4xx/5xx or transport error returns ``None`` (logged WARN) —
  never raises. The catch-up call site's ``if messages is None:
  continue`` guard depends on this exact contract.
* The default-constructed fetcher uses the 10s per-request timeout the
  catch-up path uses today.
* A duck-typed fake satisfies the :class:`ChannelHistoryFetcher`
  Protocol without inheritance — the seam PR 2 / PR 3 inject through.

The end-to-end catch-up regression (the fetcher still works *through*
``replay_channel_history``) is covered by the unchanged
``test_channel_catchup.py`` suite.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import aiohttp
import pytest
from aiohttp import web

from agents.channel_history_fetcher import (
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ChannelHistoryFetcher,
    HttpChannelHistoryFetcher,
)

# The ``orchestrator`` loopback fixture is registered via ``conftest.py``
# so it is injected by name — no per-file import (which would trip ruff
# F811 on the fixture parameter).


@asynccontextmanager
async def _serve(
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> AsyncIterator[str]:
    """Start a loopback aiohttp server exposing only the channel-history
    route, bound to ``handler``. Yields the base URL.

    Used for the failure cases (404, slow endpoint) the shared
    ``orchestrator`` fixture does not model — that fixture only fails a
    path via a 500, and never delays a response.
    """
    app = web.Application()
    app.router.add_get("/api/v1/channels/{id}/messages", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        await runner.cleanup()


class TestHttpChannelHistoryFetcherHappyPath:
    async def test_fetch_returns_messages_array(self, orchestrator):
        """``fetch`` returns the JSON payload's ``messages`` array
        verbatim — the fetcher treats each row as opaque."""
        base_url, state = orchestrator
        state["history"]["c1"] = [
            {"id": "m1", "content": "hello"},
            {"id": "m2", "content": "world"},
        ]

        async with aiohttp.ClientSession() as session:
            fetcher = HttpChannelHistoryFetcher(
                session=session, orchestrator_url=base_url,
            )
            messages = await fetcher.fetch("c1", limit=20)

        assert messages == [
            {"id": "m1", "content": "hello"},
            {"id": "m2", "content": "world"},
        ]

    async def test_fetch_passes_explicit_limit_in_query_string(
        self, orchestrator,
    ):
        """The ``limit`` argument surfaces as an explicit ``?limit=``
        query string so a server-side default cannot silently change
        ingest depth."""
        base_url, state = orchestrator
        state["history"]["c1"] = []

        async with aiohttp.ClientSession() as session:
            fetcher = HttpChannelHistoryFetcher(
                session=session, orchestrator_url=base_url,
            )
            await fetcher.fetch("c1", limit=37)

        assert any(
            p == "/api/v1/channels/c1/messages?limit=37"
            for p in state["log"]
        ), f"expected explicit ?limit=37; got {state['log']!r}"

    async def test_fetch_empty_channel_returns_empty_list(
        self, orchestrator,
    ):
        """A channel with no history → ``[]``, not ``None``. ``None`` is
        reserved for the error path so callers can tell "empty" apart
        from "fetch failed"."""
        base_url, state = orchestrator
        # ``c-empty`` is never added to ``state["history"]`` — the
        # loopback handler returns ``{"messages": []}`` for it.

        async with aiohttp.ClientSession() as session:
            fetcher = HttpChannelHistoryFetcher(
                session=session, orchestrator_url=base_url,
            )
            messages = await fetcher.fetch("c-empty", limit=50)

        assert messages == []

    async def test_fetch_trailing_slash_in_url_is_normalized(
        self, orchestrator,
    ):
        """A trailing slash on ``orchestrator_url`` must not produce a
        double slash in the request path."""
        base_url, state = orchestrator
        state["history"]["c1"] = [{"id": "m1"}]

        async with aiohttp.ClientSession() as session:
            fetcher = HttpChannelHistoryFetcher(
                session=session, orchestrator_url=base_url + "/",
            )
            messages = await fetcher.fetch("c1", limit=10)

        assert messages == [{"id": "m1"}]
        assert all("//api/v1" not in p for p in state["log"])

    async def test_fetch_round_trips_colon_bearing_channel_id(
        self, orchestrator,
    ):
        """RFC 0011 channel ids carry colons (``group:name``,
        ``dm:a:b``). ``quote(channel_id, safe='')`` percent-encodes them
        into a single path segment; the round-trip proves the encoded
        request decoded back to exactly the channel the history was
        stored under. DM channels are RFC 0034 Phase 1's whole scope, so
        a colon id must not mis-route."""
        base_url, state = orchestrator
        state["history"]["dm:ember-owl:iron-fox"] = [
            {"id": "m1", "content": "quick q"},
        ]

        async with aiohttp.ClientSession() as session:
            fetcher = HttpChannelHistoryFetcher(
                session=session, orchestrator_url=base_url,
            )
            messages = await fetcher.fetch("dm:ember-owl:iron-fox", limit=20)

        assert messages == [{"id": "m1", "content": "quick q"}]
        assert (
            "/api/v1/channels/dm:ember-owl:iron-fox/messages?limit=20"
            in state["log"]
        )

    async def test_fetcher_is_reusable_across_calls(self, orchestrator):
        """One fetcher serves many ``fetch`` calls — PR 3 constructs it
        once per agent and calls it per turn. The caller-owned
        ``aiohttp`` session is not closed between (or after) calls."""
        base_url, state = orchestrator
        state["history"]["c1"] = [{"id": "m1"}]
        state["history"]["c2"] = [{"id": "m2"}, {"id": "m3"}]

        async with aiohttp.ClientSession() as session:
            fetcher = HttpChannelHistoryFetcher(
                session=session, orchestrator_url=base_url,
            )
            first = await fetcher.fetch("c1", limit=10)
            second = await fetcher.fetch("c2", limit=10)
            assert not session.closed

        assert first == [{"id": "m1"}]
        assert second == [{"id": "m2"}, {"id": "m3"}]


class TestHttpChannelHistoryFetcherMalformedPayload:
    """A 2xx response whose ``messages`` field is absent or not a JSON
    array degrades to ``[]`` — the verbatim ``isinstance(..., list)``
    guard lifted from the catch-up helper.

    ``[]`` and not ``None`` because the request *succeeded*: ``None`` is
    reserved for the transport / HTTP-error path so a caller can tell
    "the channel has nothing" apart from "the fetch failed".
    """

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param({}, id="messages-key-absent"),
            pytest.param({"messages": None}, id="messages-null"),
            pytest.param({"messages": "oops"}, id="messages-string"),
            pytest.param({"messages": {"m1": {}}}, id="messages-object"),
            pytest.param({"messages": 7}, id="messages-int"),
        ],
    )
    async def test_shapeless_messages_field_returns_empty_list(
        self, payload,
    ):
        async def handler(_request: web.Request) -> web.StreamResponse:
            return web.json_response(payload)

        async with _serve(handler) as base_url, \
                aiohttp.ClientSession() as session:
            fetcher = HttpChannelHistoryFetcher(
                session=session, orchestrator_url=base_url,
            )
            messages = await fetcher.fetch("c1", limit=50)

        assert messages == []


class TestHttpChannelHistoryFetcherFailures:
    async def test_http_5xx_returns_none_and_logs_warning(
        self, orchestrator, caplog,
    ):
        """A 5xx is a best-effort failure: ``fetch`` returns ``None``
        and logs WARN — it never raises."""
        base_url, state = orchestrator
        state["fail_paths"].add("/api/v1/channels/c1/messages")

        with caplog.at_level("WARNING", logger="agents.channel_history_fetcher"):
            async with aiohttp.ClientSession() as session:
                fetcher = HttpChannelHistoryFetcher(
                    session=session, orchestrator_url=base_url,
                )
                messages = await fetcher.fetch("c1", limit=50)

        assert messages is None
        assert any(
            "c1" in rec.message and "HTTP 500" in rec.message
            for rec in caplog.records
        ), f"expected HTTP-500 WARN; got {[r.message for r in caplog.records]!r}"

    async def test_oversize_error_body_is_truncated_in_warning(self, caplog):
        """An error response with a large body (e.g. an orchestrator
        stack trace) is truncated to 256 chars before it reaches the
        WARN log — the verbatim ``body[:256]`` cap keeps one bad
        response from flooding the boot log."""

        async def big_error(_request: web.Request) -> web.StreamResponse:
            return web.Response(text="E" * 1000, status=500)

        with caplog.at_level("WARNING", logger="agents.channel_history_fetcher"):
            async with _serve(big_error) as base_url, \
                    aiohttp.ClientSession() as session:
                fetcher = HttpChannelHistoryFetcher(
                    session=session, orchestrator_url=base_url,
                )
                messages = await fetcher.fetch("c1", limit=50)

        assert messages is None
        warns = [r.message for r in caplog.records if "HTTP 500" in r.message]
        assert warns, "expected an HTTP-500 WARN"
        # Exactly 256 body chars survive — not 257, not the full 1000.
        assert "E" * 256 in warns[0]
        assert "E" * 257 not in warns[0]

    async def test_http_404_returns_none_and_logs_warning(self, caplog):
        """A 404 takes the same ``resp.status >= 400`` branch as a 5xx —
        ``None`` plus a WARN, no exception."""

        async def not_found(_request: web.Request) -> web.StreamResponse:
            return web.json_response({"error": "NOT_FOUND"}, status=404)

        with caplog.at_level("WARNING", logger="agents.channel_history_fetcher"):
            async with _serve(not_found) as base_url, \
                    aiohttp.ClientSession() as session:
                fetcher = HttpChannelHistoryFetcher(
                    session=session, orchestrator_url=base_url,
                )
                messages = await fetcher.fetch("gone", limit=50)

        assert messages is None
        assert any(
            "gone" in rec.message and "HTTP 404" in rec.message
            for rec in caplog.records
        ), f"expected HTTP-404 WARN; got {[r.message for r in caplog.records]!r}"

    async def test_transport_timeout_returns_none_and_logs_warning(
        self, caplog,
    ):
        """A request that exceeds the configured timeout is caught,
        logged WARN, and yields ``None`` — a slow orchestrator must not
        propagate an exception to the caller."""

        async def slow(_request: web.Request) -> web.StreamResponse:
            await asyncio.sleep(0.3)
            return web.json_response({"messages": []})

        with caplog.at_level("WARNING", logger="agents.channel_history_fetcher"):
            async with _serve(slow) as base_url, \
                    aiohttp.ClientSession() as session:
                fetcher = HttpChannelHistoryFetcher(
                    session=session,
                    orchestrator_url=base_url,
                    timeout=aiohttp.ClientTimeout(total=0.05),
                )
                messages = await fetcher.fetch("c1", limit=50)

        assert messages is None
        assert any(
            "c1" in rec.message and "failed" in rec.message
            for rec in caplog.records
        ), f"expected transport-failure WARN; got {[r.message for r in caplog.records]!r}"

    async def test_connection_refused_returns_none(self, caplog):
        """A dead orchestrator (nothing listening) is the same
        best-effort failure path — ``None``, WARN, no raise."""
        with caplog.at_level("WARNING", logger="agents.channel_history_fetcher"):
            async with aiohttp.ClientSession() as session:
                fetcher = HttpChannelHistoryFetcher(
                    session=session,
                    # Port 1 is privileged and unbound in the test env.
                    orchestrator_url="http://127.0.0.1:1",
                    timeout=aiohttp.ClientTimeout(total=1.0),
                )
                messages = await fetcher.fetch("c1", limit=50)

        assert messages is None
        assert any("failed" in rec.message for rec in caplog.records)


class TestHttpChannelHistoryFetcherTimeout:
    async def test_default_timeout_is_ten_seconds(self):
        """The default-constructed fetcher uses the same 10s per-request
        timeout the catch-up path uses today (no ``timeout`` kwarg)."""
        assert DEFAULT_REQUEST_TIMEOUT_SECONDS == 10.0
        async with aiohttp.ClientSession() as session:
            fetcher = HttpChannelHistoryFetcher(
                session=session, orchestrator_url="http://127.0.0.1:9",
            )
        assert fetcher._timeout.total == 10.0

    async def test_explicit_timeout_overrides_default(self):
        """An explicit ``timeout`` wins over the default — the catch-up
        path passes its own ``ClientTimeout`` this way."""
        async with aiohttp.ClientSession() as session:
            fetcher = HttpChannelHistoryFetcher(
                session=session,
                orchestrator_url="http://127.0.0.1:9",
                timeout=aiohttp.ClientTimeout(total=3.5),
            )
        assert fetcher._timeout.total == 3.5


class _FakeChannelHistoryFetcher:
    """Duck-typed :class:`ChannelHistoryFetcher` — no inheritance.

    Demonstrates the seam PR 2 / PR 3 inject through: a test fake is a
    plain object that just implements ``fetch``.
    """

    def __init__(self, result: list[dict[str, Any]] | None) -> None:
        self.calls: list[tuple[str, int]] = []
        self._result = result

    async def fetch(
        self, channel_id: str, *, limit: int,
    ) -> list[dict[str, Any]] | None:
        self.calls.append((channel_id, limit))
        return self._result


class TestChannelHistoryFetcherProtocol:
    async def test_duck_typed_fake_satisfies_protocol(self):
        """A fake with the right ``fetch`` shape is a structural
        :class:`ChannelHistoryFetcher` — assignment is the static
        (mypy-checked) conformance assertion."""
        fetcher: ChannelHistoryFetcher = _FakeChannelHistoryFetcher(
            [{"id": "m1"}],
        )
        result = await fetcher.fetch("c1", limit=5)
        assert result == [{"id": "m1"}]

    async def test_http_fetcher_satisfies_protocol(self):
        """The production implementation conforms to the Protocol it is
        the default binding for."""
        async with aiohttp.ClientSession() as session:
            fetcher: ChannelHistoryFetcher = HttpChannelHistoryFetcher(
                session=session, orchestrator_url="http://127.0.0.1:9",
            )
        assert callable(fetcher.fetch)


# pytest-asyncio plugin auto-detects ``async def`` tests via
# ``asyncio_mode = "auto"`` in ``pyproject.toml``; no marker needed.
