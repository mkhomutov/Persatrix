"""ISSUE-0032 (Python side) — ``channel.publish`` OTel span pinning.

Companion to the Go-side ``channel.dispatch`` span landed in PR #286
(``internal/channels/grpc_dispatcher.go`` /
``internal/channels/grpc_dispatcher_test.go``). Mirrors the same
trace contract on the Python publisher so an operator pivoting from a
trace ID can see the publish attempt's business-logic span (with
``channel.id``, ``channel.sender_id``, ``channel.mentions_count``,
``channel.message_id``) instead of only the aiohttp client span.

Status discipline (matches Go side):

* Happy path → status ``UNSET``, ``channel.message_id`` set from the
  201 response body.
* HTTP 503 (channels disabled, sticky) → exception recorded but status
  left ``UNSET``. Per RFC 0011 §C "Delivery guarantees", this is a
  deployment-wide signal, not an internal failure; flagging it as
  ``ERROR`` would inflate error-rate dashboards on every agent built
  to also run against orchestrators that have channels disabled.
* Other 4xx / 5xx → exception recorded, status ``ERROR``.
* Sticky short-circuit (no HTTP roundtrip) → exception recorded,
  status ``UNSET`` (same reasoning as the first 503).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import aiohttp
import pytest
from aiohttp import web
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from agents.channel_publisher import (
    ChannelsDisabledError,
    HTTPChannelPublisher,
)
from agents.observability.spans import CHANNEL_PUBLISH_SPAN

# ─── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def exporter() -> Iterator[InMemorySpanExporter]:
    """Install a fresh InMemorySpanExporter on the active tracer provider.

    Same shape as ``agents/tests/test_observability_spans.py::exporter`` —
    avoids ``init_tracing`` because that would build a competing provider
    and break downstream tests sharing the global.
    """
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        provider = TracerProvider()
        trace.set_tracer_provider(provider)
    exp = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exp)
    provider.add_span_processor(processor)
    yield exp
    processor.shutdown()


@pytest.fixture
async def ok_server() -> AsyncIterator[tuple[str, list[dict]]]:
    """Loopback aiohttp server that returns 201 with a generated ``id``."""
    captured: list[dict] = []

    async def handler(request: web.Request) -> web.Response:
        body = await request.json()
        captured.append({"path": request.path, "body": body})
        return web.json_response({"id": "msg-abc-123"}, status=201)

    app = web.Application()
    app.router.add_post("/api/v1/channels/{id}/messages", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    try:
        yield f"http://127.0.0.1:{port}", captured
    finally:
        await runner.cleanup()


@pytest.fixture
async def disabled_server() -> AsyncIterator[str]:
    """Loopback aiohttp server that always returns 503."""
    async def handler(request: web.Request) -> web.Response:
        return web.json_response({"error": "channels disabled"}, status=503)

    app = web.Application()
    app.router.add_post("/api/v1/channels/{id}/messages", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        await runner.cleanup()


@pytest.fixture
async def forbidden_server() -> AsyncIterator[str]:
    """Loopback aiohttp server that returns 403 (per-message NOT_MEMBER)."""
    async def handler(request: web.Request) -> web.Response:
        await request.read()
        return web.json_response({"error": "NOT_MEMBER"}, status=403)

    app = web.Application()
    app.router.add_post("/api/v1/channels/{id}/messages", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        await runner.cleanup()


# ─── helpers ─────────────────────────────────────────────────────────────────


def _publish_spans(exporter: InMemorySpanExporter):  # noqa: ANN202
    return [
        s for s in exporter.get_finished_spans()
        if s.name == CHANNEL_PUBLISH_SPAN
    ]


# ─── tests ───────────────────────────────────────────────────────────────────


class TestChannelPublishSpanHappyPath:
    """Happy path: span name, attributes, status UNSET, message_id captured."""

    async def test_emits_named_span_with_channel_attributes(
        self, exporter: InMemorySpanExporter, ok_server,
    ) -> None:
        base_url, _ = ok_server
        async with aiohttp.ClientSession() as session:
            pub = HTTPChannelPublisher(orchestrator_url=base_url, session=session)
            await pub.publish(
                channel_id="group:planning",
                sender_id="agent-a",
                content="hi",
                mentions=["agent-b", "agent-c"],
            )

        spans = _publish_spans(exporter)
        assert len(spans) == 1, (
            f"expected exactly one {CHANNEL_PUBLISH_SPAN} span, "
            f"got {[s.name for s in exporter.get_finished_spans()]}"
        )
        span = spans[0]
        assert span.attributes["channel.id"] == "group:planning"
        assert span.attributes["channel.sender_id"] == "agent-a"
        assert span.attributes["channel.mentions_count"] == 2

    async def test_status_is_unset_on_success(
        self, exporter: InMemorySpanExporter, ok_server,
    ) -> None:
        base_url, _ = ok_server
        async with aiohttp.ClientSession() as session:
            pub = HTTPChannelPublisher(orchestrator_url=base_url, session=session)
            await pub.publish(
                channel_id="group:planning",
                sender_id="agent-a",
                content="hi",
                mentions=[],
            )

        span = _publish_spans(exporter)[0]
        # OTEL's StatusCode.UNSET is the post-construction default; a
        # successful operation must NOT promote it to OK or ERROR.
        # Querying ``status_code.name`` keeps the assertion readable in
        # failure output.
        assert span.status.status_code.name == "UNSET", (
            f"expected UNSET on happy path, got {span.status.status_code.name}"
        )
        # No exception event.
        assert "exception" not in [e.name for e in span.events]

    async def test_message_id_captured_from_response(
        self, exporter: InMemorySpanExporter, ok_server,
    ) -> None:
        """The orchestrator returns ``{"id": "..."}`` on 201; the span
        records it as ``channel.message_id`` so the publisher span can be
        joined in trace storage to the Go-side ``channel.dispatch`` span
        (which carries the same id under the same attribute key)."""
        base_url, _ = ok_server
        async with aiohttp.ClientSession() as session:
            pub = HTTPChannelPublisher(orchestrator_url=base_url, session=session)
            await pub.publish(
                channel_id="group:planning",
                sender_id="agent-a",
                content="hi",
                mentions=[],
            )

        span = _publish_spans(exporter)[0]
        assert span.attributes.get("channel.message_id") == "msg-abc-123"

    async def test_zero_mentions_recorded_as_zero_count(
        self, exporter: InMemorySpanExporter, ok_server,
    ) -> None:
        base_url, _ = ok_server
        async with aiohttp.ClientSession() as session:
            pub = HTTPChannelPublisher(orchestrator_url=base_url, session=session)
            await pub.publish(
                channel_id="group:planning",
                sender_id="agent-a",
                content="hi",
                mentions=[],
            )

        span = _publish_spans(exporter)[0]
        # ``0`` not absent — operator queries `mentions_count == 0` to
        # find no-mention publishes; missing attribute would force a
        # backend-specific "absent or zero" filter.
        assert span.attributes["channel.mentions_count"] == 0


class TestChannelPublishSpanFailures:
    """Error paths: span records the exception with the right status."""

    async def test_403_marks_span_error_and_records_exception(
        self, exporter: InMemorySpanExporter, forbidden_server,
    ) -> None:
        base_url = forbidden_server
        async with aiohttp.ClientSession() as session:
            pub = HTTPChannelPublisher(orchestrator_url=base_url, session=session)
            with pytest.raises(aiohttp.ClientResponseError):
                await pub.publish(
                    channel_id="forbidden",
                    sender_id="agent-a",
                    content="hi",
                    mentions=[],
                )

        span = _publish_spans(exporter)[0]
        assert span.status.status_code.name == "ERROR"
        assert "exception" in [e.name for e in span.events]

    async def test_503_records_exception_but_leaves_status_unset(
        self, exporter: InMemorySpanExporter, disabled_server,
    ) -> None:
        """503 channels-disabled is a deployment signal, not an error.

        Marking the span ``ERROR`` would inflate trace error-rate
        dashboards every time an agent is run against an orchestrator
        with channels off (a supported v0.3.0 deployment shape per the
        sticky-disabled flag — see ISSUE-0026). Mirrors the Go-side
        ``channel.dispatch`` discipline of leaving ``Unset`` on
        best-effort no-ops; the exception event remains so operators
        can still pivot to "find the disabled traces" via an
        event-name query.
        """
        base_url = disabled_server
        async with aiohttp.ClientSession() as session:
            pub = HTTPChannelPublisher(orchestrator_url=base_url, session=session)
            with pytest.raises(ChannelsDisabledError):
                await pub.publish(
                    channel_id="group:planning",
                    sender_id="agent-a",
                    content="hi",
                    mentions=[],
                )

        span = _publish_spans(exporter)[0]
        assert span.status.status_code.name == "UNSET", (
            "503 channels-disabled is a sticky deployment signal; the span "
            "must not be ERROR or it inflates error-rate dashboards on "
            "every channels-off run"
        )
        # Operator pivot: the exception event is still on the span so
        # `event.name = "exception"` queries surface the disabled traces.
        assert "exception" in [e.name for e in span.events]

    async def test_sticky_short_circuit_emits_span_with_unset_status(
        self, exporter: InMemorySpanExporter, disabled_server,
    ) -> None:
        """Subsequent calls after sticky-disable still emit a span.

        The HTTP roundtrip is short-circuited, but the publisher span
        must still record the publish *attempt* — without it, an
        operator looking at the action executor's parent span sees
        "agent decided to publish" with no child evidence of the
        attempt. The span carries the same UNSET status as the first
        503 (same deployment signal) and the ``ChannelsDisabledError``
        is recorded as an exception event.
        """
        base_url = disabled_server
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
            exporter.clear()
            # Short-circuit: no HTTP, but a span must still appear.
            with pytest.raises(ChannelsDisabledError):
                await pub.publish(
                    channel_id="group:planning",
                    sender_id="agent-a",
                    content="again",
                    mentions=[],
                )

        spans = _publish_spans(exporter)
        assert len(spans) == 1, (
            "short-circuited publish must still emit one channel.publish span"
        )
        assert spans[0].status.status_code.name == "UNSET"
        assert "exception" in [e.name for e in spans[0].events]
        # Attributes still pinned even when no wire call happened.
        assert spans[0].attributes["channel.id"] == "group:planning"
        assert spans[0].attributes["channel.sender_id"] == "agent-a"

    async def test_empty_channel_id_value_error_marks_span_error(
        self, exporter: InMemorySpanExporter,
    ) -> None:
        """Defensive guard: empty channel_id raises before any HTTP work,
        but the span must still mark the failure so operators see it in
        traces rather than losing the action's tail end entirely."""
        async with aiohttp.ClientSession() as session:
            pub = HTTPChannelPublisher(
                orchestrator_url="http://x", session=session,
            )
            with pytest.raises(ValueError, match="channel_id"):
                await pub.publish(
                    channel_id="",
                    sender_id="agent-a",
                    content="hi",
                    mentions=[],
                )

        span = _publish_spans(exporter)[0]
        assert span.status.status_code.name == "ERROR"
        assert "exception" in [e.name for e in span.events]
