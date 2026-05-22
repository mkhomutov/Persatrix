"""Tests for cascade_depth wire propagation in :class:`HTTPChannelPublisher`.

RFC 0011 amendment "Cascade-depth wire propagation" (PR 3 of the v0.3.0
channel test-findings plan):

* The publisher accepts a ``cascade_depth: int`` kwarg.
* When the value is non-zero it MUST land in the POST body's
  ``metadata`` map under the key ``cascade_depth`` so the orchestrator's
  publish handler can clamp + thread it onto the fanout (PR 2).
* When the publisher receives the default (zero) the entire ``metadata``
  map MUST be omitted from the POST body — proto3 implicit presence on
  the gRPC side already conflates "unset" with "zero", and an empty
  REST ``metadata`` map on every publish is operationally noisy without
  carrying any signal.

The companion REST→fanout enforcement was added to the orchestrator in
PR 2 (#319); this file pins the Python emitter side of the same
contract.
"""

from __future__ import annotations

import aiohttp
import pytest
from aiohttp import web

from agents.channel_publisher import HTTPChannelPublisher


@pytest.fixture
async def captured_server():
    """Start a loopback aiohttp server that records every POST body.

    Same shape as the fixture in ``test_http_channel_publisher.py``;
    duplicated here (rather than promoted to ``conftest.py``) so this
    file stands alone as the cascade-depth contract pin.
    """
    captured: list[dict] = []

    async def handler(request: web.Request) -> web.Response:
        body = await request.json()
        captured.append({"path": request.path, "body": body})
        return web.json_response({"id": "m-1"}, status=201)

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


class TestCascadeDepthOnTheWire:

    async def test_nonzero_cascade_depth_lands_in_metadata(self, captured_server):
        """A non-zero ``cascade_depth`` rides in the POST body's ``metadata`` map.

        The orchestrator's publish handler reads ``req.Metadata["cascade_depth"]``
        (PR 2 #319), clamps to ``[0, max_cascade_depth]``, and uses it to
        drive the fanout-cap decision. A missing or zero value collapses
        to cascade-origin semantics on the orchestrator side.
        """
        base_url, captured = captured_server
        async with aiohttp.ClientSession() as session:
            pub = HTTPChannelPublisher(orchestrator_url=base_url, session=session)
            await pub.publish(
                channel_id="group:planning",
                sender_id="agent-a",
                content="hi",
                mentions=[],
                cascade_depth=3,
            )
        assert len(captured) == 1
        body = captured[0]["body"]
        assert "metadata" in body, (
            f"non-zero cascade_depth must land in body.metadata; "
            f"got body={body!r}"
        )
        assert body["metadata"] == {"cascade_depth": 3}, (
            f"metadata must carry cascade_depth verbatim; got {body['metadata']!r}"
        )

    async def test_zero_cascade_depth_omits_metadata_entirely(self, captured_server):
        """``cascade_depth=0`` MUST NOT add an empty ``metadata`` map.

        Proto3 implicit presence: an unset ``cascade_depth`` on the gRPC
        side is indistinguishable from explicit zero, and zero is the
        cascade-origin value. Emitting ``"metadata": {}`` on every publish
        would be operational noise — the orchestrator's REST handler
        defaults missing metadata to depth 0 anyway.
        """
        base_url, captured = captured_server
        async with aiohttp.ClientSession() as session:
            pub = HTTPChannelPublisher(orchestrator_url=base_url, session=session)
            await pub.publish(
                channel_id="group:planning",
                sender_id="agent-a",
                content="hi",
                mentions=[],
                cascade_depth=0,
            )
        assert len(captured) == 1
        body = captured[0]["body"]
        assert "metadata" not in body, (
            f"cascade_depth=0 (cascade-origin) must NOT emit "
            f"metadata={{}}; got body={body!r}"
        )

    async def test_default_cascade_depth_emits_terminate_at_cap(self, captured_server):
        """Callers that omit ``cascade_depth`` publish at the cap.

        The kwarg default flipped from ``0`` (chain-origin, omit
        metadata) to :data:`DEFAULT_MAX_CASCADE_DEPTH` (terminate at the
        orchestrator clamp, include metadata) because the previous
        default silently masked the v0.3.0 tick-scheduler regression:
        every channel message woke the tick loop, the woken tick
        published at depth 0, and the orchestrator's per-hop cap never
        fired. The new default is the "no inbound depth known" sentinel
        — the publish lands at the cap so the orchestrator's
        ``cascade_depth >= max_cascade_depth`` clamp drops fanout and
        the chain terminates. Call sites that legitimately mark a
        publish as chain-origin (chat surface, dispatcher's first hop)
        pass ``cascade_depth=0`` explicitly; the explicit-zero contract
        is pinned by ``test_explicit_zero_omits_metadata`` above.
        """
        from agents.cascade_depth_defaults import DEFAULT_MAX_CASCADE_DEPTH

        base_url, captured = captured_server
        async with aiohttp.ClientSession() as session:
            pub = HTTPChannelPublisher(orchestrator_url=base_url, session=session)
            await pub.publish(
                channel_id="group:planning",
                sender_id="agent-a",
                content="hi",
                mentions=[],
            )
        body = captured[0]["body"]
        assert body.get("metadata") == {"cascade_depth": DEFAULT_MAX_CASCADE_DEPTH}, (
            "publisher.publish() without cascade_depth must emit "
            f"metadata.cascade_depth={DEFAULT_MAX_CASCADE_DEPTH} so the "
            "orchestrator clamps and drops fanout on tick-originated "
            f"publishes; got body={body!r}"
        )

    async def test_cascade_depth_coexists_with_mentions(self, captured_server):
        """``cascade_depth`` and ``mentions`` ride the wire independently.

        The orchestrator consumes ``mentions`` for fan-out routing and
        ``metadata.cascade_depth`` for the fan-out cap — distinct
        signals. The publisher must not collapse one into the other.
        """
        base_url, captured = captured_server
        async with aiohttp.ClientSession() as session:
            pub = HTTPChannelPublisher(orchestrator_url=base_url, session=session)
            await pub.publish(
                channel_id="group:planning",
                sender_id="agent-a",
                content="hi",
                mentions=["agent-b", "agent-c"],
                cascade_depth=2,
            )
        body = captured[0]["body"]
        assert body["mentions"] == ["agent-b", "agent-c"]
        assert body["metadata"] == {"cascade_depth": 2}


class TestCustomMetadataPassThrough:
    """ISSUE-0065 — extra ``metadata`` rides the wire alongside ``cascade_depth``.

    The chat-error reply published by the inbound channel-event
    processing path (``agents.chat_reply.process_inbound_channel_event``)
    under :class:`BudgetExceededError` carries a ``metadata['reply_status']``
    discriminator that the orchestrator's REST chat handler reads to
    set ``reply_status='error'`` in the JSON envelope. The publisher
    must therefore accept caller-supplied metadata and merge it with
    the cascade_depth seat without dropping or overwriting either side.
    """

    async def test_custom_metadata_lands_in_wire_payload(self, captured_server):
        base_url, captured = captured_server
        async with aiohttp.ClientSession() as session:
            pub = HTTPChannelPublisher(orchestrator_url=base_url, session=session)
            await pub.publish(
                channel_id="dm:alice:chat-agent",
                sender_id="chat-agent",
                content="per_agent budget exceeded",
                mentions=["alice"],
                cascade_depth=0,
                metadata={"reply_status": "error", "error_reason": "budget_exceeded"},
            )
        body = captured[0]["body"]
        assert body.get("metadata") is not None, (
            "ISSUE-0065: caller-supplied metadata must land on the wire; "
            f"got body={body!r}"
        )
        assert body["metadata"].get("reply_status") == "error"
        assert body["metadata"].get("error_reason") == "budget_exceeded"

    async def test_custom_metadata_coexists_with_cascade_depth(self, captured_server):
        base_url, captured = captured_server
        async with aiohttp.ClientSession() as session:
            pub = HTTPChannelPublisher(orchestrator_url=base_url, session=session)
            await pub.publish(
                channel_id="group:planning",
                sender_id="agent-a",
                content="hi",
                mentions=[],
                cascade_depth=2,
                metadata={"reply_status": "error"},
            )
        body = captured[0]["body"]
        assert body["metadata"]["cascade_depth"] == 2
        assert body["metadata"]["reply_status"] == "error"

    async def test_caller_cascade_depth_in_metadata_is_dropped_kwarg_wins(
        self, captured_server,
    ):
        """Caller-supplied ``metadata['cascade_depth']`` is silently dropped.

        PR #395 review finding — the publisher's docstring declares
        ``cascade_depth`` a reserved metadata key (the kwarg is the
        canonical seat). Pin the reserved-key invariant so a future
        refactor can't quietly start honouring metadata-side overrides
        — that would let any caller bypass the cascade clamp.
        """
        base_url, captured = captured_server
        async with aiohttp.ClientSession() as session:
            pub = HTTPChannelPublisher(orchestrator_url=base_url, session=session)
            await pub.publish(
                channel_id="group:planning",
                sender_id="agent-a",
                content="hi",
                mentions=[],
                cascade_depth=2,
                # Hostile/buggy caller tries to override via metadata.
                metadata={"cascade_depth": 99, "reply_status": "error"},
            )
        body = captured[0]["body"]
        assert body["metadata"]["cascade_depth"] == 2, (
            "kwarg cascade_depth must win over caller metadata; "
            f"got metadata={body['metadata']!r}"
        )
        assert body["metadata"]["reply_status"] == "error"

    async def test_caller_cascade_depth_in_metadata_dropped_at_origin(
        self, captured_server,
    ):
        """Reserved-key drop also applies at cascade-origin (``cascade_depth=0``).

        At depth 0 the publisher omits the ``cascade_depth`` key entirely
        from the wire (origin is indistinguishable from unset in proto3);
        the caller's metadata-side ``cascade_depth`` must NOT sneak in
        through the merge as a substitute.
        """
        base_url, captured = captured_server
        async with aiohttp.ClientSession() as session:
            pub = HTTPChannelPublisher(orchestrator_url=base_url, session=session)
            await pub.publish(
                channel_id="dm:alice:chat-agent",
                sender_id="chat-agent",
                content="hi",
                mentions=[],
                cascade_depth=0,
                metadata={"cascade_depth": 7, "reply_status": "error"},
            )
        body = captured[0]["body"]
        assert "cascade_depth" not in body["metadata"], (
            "reserved-key invariant: caller metadata['cascade_depth'] must "
            "not appear on the wire when kwarg cascade_depth=0; got "
            f"metadata={body['metadata']!r}"
        )
        assert body["metadata"]["reply_status"] == "error"
