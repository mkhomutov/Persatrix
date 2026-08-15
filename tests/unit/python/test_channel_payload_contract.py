"""RFC 0040 Phase 1 contract pin for the channel publish/history payloads.

[RFC 0040](docs/rfcs/0040-agent-orchestrator-transport-unification.md)
Motivation 1 names the risk this file closes: during the Phases 2–3
dual-surface window the agent and the orchestrator speak REST *and*
gRPC at once, and two payload schemas maintained in two languages drift
in parallel with nothing to catch it. Phase 1's answer is to pin the
contract over **today's** REST path first, so the dual-surface window
opens from a verified shape rather than an assumed one.

Three layers, deliberately separate:

* **Shape pins** — a representative publish body and history envelope
  validate against ``schemas/channel.schema.json``; a drifted field
  fails. These fail if the *schema* is wrong.
* **Round-trip** — the real :class:`HTTPChannelPublisher` publishes to a
  loopback server and the body that actually went on the wire is
  validated. This fails if the *producer* drifts from the schema, which
  a hand-written sample payload could never catch.
* **Fail-open** — the validator's contract is that a violation logs and
  returns; it never raises. Pinned because the publish path's resilience
  now depends on it (RFC 0040's "no flag day" guarantee).

The Go decode side is pinned separately, by struct-tag parity, in
``test_cross_language_channel_payload_drift.py`` — see that module's
docstring for why the orchestrator side is a test-time pin rather than
runtime validation.
"""

from __future__ import annotations

import logging

import aiohttp
import jsonschema  # type: ignore[import-untyped]
import pytest
from aiohttp import web

from agents.channel_payload_contract import (
    channel_payload_schema,
    subschema,
    validate_publish_payload,
)
from agents.channel_publisher import HTTPChannelPublisher


def _validate(instance: object, definition: str) -> None:
    """Validate ``instance`` against a named definition, or fail the test.

    A missing schema/definition is a hard failure here (unlike the
    fail-open runtime path): in the test environment the repo tree is
    present, so ``None`` means the schema or the definition name has
    gone missing — precisely the drift this file exists to catch, and
    silently skipping would make the whole module a no-op.
    """
    sub = subschema(definition)
    assert sub is not None, (
        f"definition {definition!r} not resolvable from "
        "schemas/channel.schema.json — the contract pin cannot run"
    )
    jsonschema.validate(instance=instance, schema=sub)


# ─── Representative payloads ──────────────────────────────────────────
# Mirrors of what the two sides actually exchange, kept minimal-but-real:
# every optional field appears in at least one sample so the schema's
# `additionalProperties: false` is exercised against the full surface,
# not just the four fields RFC 0040 enumerated in 2026-07.

_FULL_PUBLISH = {
    "sender_id": "agent-a",
    "content": "hello",
    "thread_id": "m-parent",
    "mentions": ["agent-b"],
    "channel_type": "group",
    "metadata": {"cascade_depth": 2, "interaction_id": "01J0"},
    "session_id": "sess-1",
    "epoch_id": "epoch-1",
}

_MINIMAL_PUBLISH = {"sender_id": "agent-a", "content": "hello"}

_HISTORY_ENVELOPE = {
    "messages": [
        {
            "id": "m-1",
            "channel_id": "group:planning",
            "sender_id": "agent-a",
            "content": "hello",
            "timestamp": "2026-08-15T10:00:00Z",
            "mentions": [],
        },
        {
            "id": "m-2",
            "channel_id": "group:planning",
            "sender_id": "agent-b",
            "content": "reply",
            "timestamp": "2026-08-15T10:00:01Z",
            "thread_id": "m-1",
            "mentions": ["agent-a"],
            "metadata": {"cascade_depth": 1},
        },
    ],
    "classification": "internal",
}


class TestPublishRequestShape:

    def test_full_publish_body_validates(self):
        """Every field the Go struct declares is accepted."""
        _validate(_FULL_PUBLISH, "publishMessageRequest")

    def test_minimal_publish_body_validates(self):
        """`sender_id` + `content` alone is a complete publish."""
        _validate(_MINIMAL_PUBLISH, "publishMessageRequest")

    @pytest.mark.parametrize("missing", ["sender_id", "content"])
    def test_required_fields_are_required(self, missing):
        payload = {k: v for k, v in _MINIMAL_PUBLISH.items() if k != missing}
        with pytest.raises(jsonschema.ValidationError):
            _validate(payload, "publishMessageRequest")

    def test_drifted_field_is_rejected(self):
        """The drift trip-wire: an unknown key fails rather than shipping.

        This is the assertion RFC 0040 Phase 1 is really about — a field
        added on one side without the other is caught at `make test`
        rather than during the dual-surface window.
        """
        drifted = {**_MINIMAL_PUBLISH, "priority": "high"}
        with pytest.raises(jsonschema.ValidationError):
            _validate(drifted, "publishMessageRequest")

    def test_empty_sender_id_is_rejected(self):
        """`sender_id` is REQUIRED and non-empty — the orchestrator does
        not infer sender identity over REST."""
        with pytest.raises(jsonschema.ValidationError):
            _validate({**_MINIMAL_PUBLISH, "sender_id": ""}, "publishMessageRequest")

    def test_empty_content_is_permitted(self):
        """Emptiness policy belongs to the orchestrator, not the contract.

        Pinned as an explicit allowance so a later tightening is a
        deliberate edit here rather than an incidental one.
        """
        _validate({**_MINIMAL_PUBLISH, "content": ""}, "publishMessageRequest")

    def test_negative_cascade_depth_is_rejected(self):
        """Inherited from `messageMetadata` via `$ref` — proves the
        cross-definition reference actually resolves."""
        payload = {**_MINIMAL_PUBLISH, "metadata": {"cascade_depth": -1}}
        with pytest.raises(jsonschema.ValidationError):
            _validate(payload, "publishMessageRequest")

    def test_large_cascade_depth_round_trips(self):
        """Above-cap values are accepted and clamped server-side, so a
        publisher need not know the deployment's cap to compose a body."""
        payload = {**_MINIMAL_PUBLISH, "metadata": {"cascade_depth": 9999}}
        _validate(payload, "publishMessageRequest")


class TestHistoryResponseShape:

    def test_history_envelope_validates(self):
        _validate(_HISTORY_ENVELOPE, "channelHistoryResponse")

    def test_empty_history_validates(self):
        _validate({"messages": [], "classification": "internal"}, "channelHistoryResponse")

    def test_empty_classification_is_accepted(self):
        """`classification` carries no `omitempty`; empty resolves to the
        `public` acting floor receiver-side (RFC 0037 §A rule (b))."""
        _validate({"messages": [], "classification": ""}, "channelHistoryResponse")

    def test_null_mentions_is_rejected(self):
        """The response builder normalises a nil slice to `[]`
        (internal/server/channel_response_builders.go). That
        normalisation is load-bearing for every client that iterates
        without a null guard — this is the test that fails if it is
        removed and `mentions` starts marshalling as `null`.
        """
        envelope = {
            "messages": [
                {**_HISTORY_ENVELOPE["messages"][0], "mentions": None},
            ],
            "classification": "internal",
        }
        with pytest.raises(jsonschema.ValidationError):
            _validate(envelope, "channelHistoryResponse")

    def test_drifted_message_field_is_rejected(self):
        envelope = {
            "messages": [
                {**_HISTORY_ENVELOPE["messages"][0], "edited_at": "2026-08-15T10:00:00Z"},
            ],
            "classification": "internal",
        }
        with pytest.raises(jsonschema.ValidationError):
            _validate(envelope, "channelHistoryResponse")

    def test_bad_classification_is_rejected(self):
        with pytest.raises(jsonschema.ValidationError):
            _validate(
                {"messages": [], "classification": "top-secret"},
                "channelHistoryResponse",
            )


@pytest.fixture
async def captured_server():
    """Loopback server recording every POST body.

    Same shape as the fixture in ``test_channel_publisher_cascade_depth.py``
    / ``test_http_channel_publisher.py``; duplicated per this suite's
    established convention so each contract file stands alone.
    """
    captured: list[dict] = []

    async def handler(request: web.Request) -> web.Response:
        captured.append(await request.json())
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


class TestProducerRoundTrip:
    """The body that actually goes on the wire validates.

    A hand-written sample proves the schema is self-consistent; only
    driving the real producer proves the *producer* still matches it.
    """

    async def test_plain_publish_body_validates(self, captured_server):
        base_url, captured = captured_server
        async with aiohttp.ClientSession() as session:
            pub = HTTPChannelPublisher(orchestrator_url=base_url, session=session)
            await pub.publish(
                channel_id="group:planning",
                sender_id="agent-a",
                content="hello",
                mentions=[],
                cascade_depth=0,
            )
        assert len(captured) == 1
        _validate(captured[0], "publishMessageRequest")
        # The zero-cascade shape rule (RFC 0011 amendment): no empty
        # `metadata` map rides along on a routine publish.
        assert "metadata" not in captured[0]

    async def test_cascade_and_mentions_body_validates(self, captured_server):
        base_url, captured = captured_server
        async with aiohttp.ClientSession() as session:
            pub = HTTPChannelPublisher(orchestrator_url=base_url, session=session)
            await pub.publish(
                channel_id="group:planning",
                sender_id="agent-a",
                content="hello",
                mentions=["agent-b"],
                cascade_depth=3,
                metadata={"interaction_id": "01J0", "end_interaction_vote": True},
            )
        assert len(captured) == 1
        body = captured[0]
        _validate(body, "publishMessageRequest")
        assert body["metadata"]["cascade_depth"] == 3
        assert body["mentions"] == ["agent-b"]


class TestFailOpenContract:
    """A contract violation must never break publishing."""

    def test_violation_returns_errors_without_raising(self, caplog):
        with caplog.at_level(logging.WARNING):
            errors = validate_publish_payload({"content": "no sender"})
        assert errors, "a body missing `sender_id` must report a violation"
        assert any("sender_id" in e for e in errors)
        assert any(
            "RFC 0040" in rec.message or "contract" in rec.message
            for rec in caplog.records
        ), "a violation must be logged, not swallowed"

    def test_clean_payload_reports_nothing(self, caplog):
        with caplog.at_level(logging.WARNING):
            assert validate_publish_payload(dict(_FULL_PUBLISH)) == []
        assert not caplog.records, "a clean payload must not log"

    def test_schema_is_loadable(self):
        """Guards the whole fail-open design: if this ever returns None
        in-repo, every runtime validation has silently become a no-op."""
        assert channel_payload_schema() is not None
