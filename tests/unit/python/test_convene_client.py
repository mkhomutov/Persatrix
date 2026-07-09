"""RFC 0052 §E — the persona-side convene trigger client (v0.3.11 PR 7c-ii-a).

Pins that :class:`agents.convene_client.HTTPConveneClient` POSTs to the SAME
``/api/v1/channels/{id}/convene`` endpoint the CLI verb and web button hit — so
a timer-fired convene passes the SAME §E aggregate ceilings
(``max_convenings`` / ``standing_budget_tokens``) the manual path does; those
bounds live in the Go ``ChannelRouter.ConveneChannel``, not the caller, so
routing through the endpoint is what keeps the fired schedule bounded.

No live orchestrator here: a minimal fake session records the request shape and
lets each test dial the response status.
"""

from __future__ import annotations

import types
from typing import Any

import aiohttp
import pytest

from agents.convene_client import HTTPConveneClient


class _FakeResponse:
    def __init__(self, status: int, url: str, body: str = "") -> None:
        self.status = status
        self._url = url
        self._body = body

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def text(self) -> str:
        return self._body

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise aiohttp.ClientResponseError(
                request_info=types.SimpleNamespace(  # type: ignore[arg-type]
                    real_url=self._url, url=self._url, method="POST", headers={},
                ),
                history=(),
                status=self.status,
            )


class _FakeSession:
    """Records ``post`` calls and returns a response with a dialled status."""

    def __init__(self, status: int = 202, body: str = "") -> None:
        self._status = status
        self._body = body
        self.calls: list[dict[str, Any]] = []

    def post(
        self, url: str, *, timeout: Any = None, **kwargs: Any
    ) -> _FakeResponse:
        self.calls.append({"url": url, "timeout": timeout, "kwargs": kwargs})
        return _FakeResponse(self._status, url, self._body)


@pytest.mark.asyncio
async def test_convene_posts_to_the_convene_endpoint() -> None:
    session = _FakeSession(status=202)
    client = HTTPConveneClient(
        orchestrator_url="http://orch:8080/",
        session=session,  # type: ignore[arg-type]  # structural fake
    )

    await client.convene("group:planning")

    assert len(session.calls) == 1
    # SAME endpoint as the CLI/web convene, channel-id path-encoded exactly as
    # the message publisher encodes it (so a colon can never escape the segment).
    assert session.calls[0]["url"] == (
        "http://orch:8080/api/v1/channels/group%3Aplanning/convene"
    )


@pytest.mark.asyncio
async def test_convene_raises_on_non_2xx() -> None:
    # A §E aggregate bound reached (429) — or 409 already-convening / 503
    # convener-unreachable — surfaces as ClientResponseError so the caller (the
    # wake handler) can log-and-drop it rather than crash the event loop.
    session = _FakeSession(status=429, body='{"error":"standing bound reached"}')
    client = HTTPConveneClient(
        orchestrator_url="http://orch:8080",
        session=session,  # type: ignore[arg-type]  # structural fake
    )

    with pytest.raises(aiohttp.ClientResponseError):
        await client.convene("group:planning")


@pytest.mark.asyncio
async def test_convene_rejects_empty_channel_id() -> None:
    session = _FakeSession()
    client = HTTPConveneClient(
        orchestrator_url="http://orch:8080",
        session=session,  # type: ignore[arg-type]  # structural fake
    )

    with pytest.raises(ValueError):
        await client.convene("")

    assert session.calls == [], "no request is issued for an empty channel id"
