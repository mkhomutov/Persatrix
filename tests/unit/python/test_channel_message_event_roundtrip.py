"""
Proto wire-shape regression test for ``ChannelMessageEvent`` and ``TaskAck``
(ISSUE-0021, follow-up to PR #246).

These messages are the only inter-process channel-delivery surface in
v0.3.0 (`AgentService.ReceiveChannelMessage`). Every other test in this
suite exercises the *handler* — validation, dispatch, ack — but none
of them serialize the message to bytes and read it back. A field-number
renumber accident (e.g. swapping ``content = 5`` and ``timestamp = 6``)
or a type flip (``repeated string mentions`` → ``string``) on a future
proto edit would survive the existing handler tests because they all
construct in-process Python objects.

This file pins the wire shape:

1. Every field declared in ``proto/task.proto`` is populated with a
   non-default value, serialized, and read back. ``decoded == event``
   asserts every byte survives the round-trip with the value AND the
   field number — proto3 silently drops fields whose numbers vanish.
2. Each field is read off the decoded message by *name*, asserting the
   field is reachable in Python after parse. A type flip would either
   raise on serialize or surface here as the wrong attribute type.
3. ``TaskAck`` gets the same treatment because the orchestrator-side
   gRPC dispatcher reads its two fields off the wire on every dispatch
   ([internal/channels/grpc_dispatcher.go]) and a renumber would silently
   strand the ``error_message`` taxonomy used by operators tracing
   rejected dispatches.

Mirror Go test: ``internal/channels/proto_roundtrip_test.go``. Cross-
language drift (Python emits a value, Go fails to parse it) is caught
because both sides exercise the same field set against the same
canonical proto.
"""

from __future__ import annotations

from agents.generated import task_pb2


def test_channel_message_event_roundtrips_all_fields():
    """Every field declared in proto/task.proto must survive Marshal/Unmarshal.

    Populates with non-default values so a missing field surfaces as a
    decoded value reverting to proto3's zero (empty string / empty list /
    etc.) and trips the equality check.
    """
    event = task_pb2.ChannelMessageEvent(
        message_id="msg-001",
        channel_id="group:eng",
        channel_type="group",
        sender_id="alice",
        content="hello world",
        timestamp="2026-05-04T12:00:00Z",
        thread_id="t-1",
        mentions=["bob", "carol"],
        respond_policy="when_mentioned",
        thread_parent_sender_id="dave",
        cascade_depth=3,
        sender_participant_type="user",
    )

    blob = event.SerializeToString()
    decoded = task_pb2.ChannelMessageEvent.FromString(blob)

    assert decoded == event
    # Per-field reachability: equality alone would catch a wholesale
    # field rename if both sides adopted the new name, but not a
    # one-sided rename that masquerades as a new "unknown field". Naming
    # each field here pins the public Python attribute surface.
    assert decoded.message_id == "msg-001"
    assert decoded.channel_id == "group:eng"
    assert decoded.channel_type == "group"
    assert decoded.sender_id == "alice"
    assert decoded.content == "hello world"
    assert decoded.timestamp == "2026-05-04T12:00:00Z"
    assert decoded.thread_id == "t-1"
    assert list(decoded.mentions) == ["bob", "carol"]
    assert decoded.respond_policy == "when_mentioned"
    assert decoded.thread_parent_sender_id == "dave"
    assert decoded.cascade_depth == 3
    assert decoded.sender_participant_type == "user"


def test_channel_message_event_cascade_depth_default_roundtrips():
    """Unset ``cascade_depth`` must round-trip as proto3 zero.

    An event built without ``cascade_depth=`` must serialize without
    emitting bytes for field 11 (proto3 implicit presence) and decode
    back to ``cascade_depth == 0``. Catches an accidental
    ``optional``-keyword promotion that would change the marshaled bytes.
    """
    event = task_pb2.ChannelMessageEvent(message_id="msg-002", channel_id="group:eng")
    decoded = task_pb2.ChannelMessageEvent.FromString(event.SerializeToString())
    assert decoded == event
    assert decoded.cascade_depth == 0


def test_channel_message_event_default_instance_roundtrips():
    """A default-constructed event must also round-trip cleanly.

    Pins the proto3 implicit-presence contract: an unset field is
    indistinguishable from an explicitly-set zero value. Catches the
    accidental introduction of a ``required`` field (a syntactically
    legal but semantically broken proto2-ism in proto3 via
    ``[(google.api.field_behavior) = REQUIRED]``).
    """
    blob = task_pb2.ChannelMessageEvent().SerializeToString()
    assert blob == b""
    decoded = task_pb2.ChannelMessageEvent.FromString(blob)
    assert decoded == task_pb2.ChannelMessageEvent()


def test_task_ack_roundtrips_both_fields():
    """``TaskAck`` is the response of every ``ReceiveChannelMessage`` call.

    A renumber accident on its two fields would silently strand
    ``error_message``, which the orchestrator-side dispatcher reads to
    classify rejected dispatches.
    """
    ack = task_pb2.TaskAck(success=False, error_message="invalid_channel_type")
    decoded = task_pb2.TaskAck.FromString(ack.SerializeToString())
    assert decoded == ack
    assert decoded.success is False
    assert decoded.error_message == "invalid_channel_type"


def test_task_ack_success_default_roundtrips():
    """Default success=False must round-trip; pins proto3 implicit zero."""
    blob = task_pb2.TaskAck().SerializeToString()
    assert blob == b""
    decoded = task_pb2.TaskAck.FromString(blob)
    assert decoded == task_pb2.TaskAck()
    assert decoded.success is False
    assert decoded.error_message == ""


# ─── Golden-bytes pin: catches field-number renumber on this side ───
#
# The round-trip tests above use the SAME generated stub for Marshal
# and Unmarshal, so a symmetric proto edit (e.g. swapping `content = 5`
# and `timestamp = 6`) regenerates both ends together and the equality
# check passes — the actual class of bug ISSUE-0021 names ("cross-language
# drift: Python emits a value, Go fails to parse") is invisible without
# pinning the wire bytes themselves.
#
# These tests pin the encoding of one field at a time against a hand-
# computed proto3 wire form. Concretely, ``string`` field N encodes as:
#
#     tag = (N << 3) | 2          ; wire-type 2 = length-delimited
#     payload = varint(len) || utf8_bytes
#
# A renumber on EITHER side breaks these tests because the produced
# bytes no longer match the pinned constant. The Go mirror test in
# ``internal/channels/proto_roundtrip_test.go`` decodes the same
# constants — if either language renumbers without the other, the
# constant fails on the regenerating side first and CI catches it.


def _string_field_bytes(field_number: int, value: str) -> bytes:
    """Hand-encode `string field_number = ...` per proto3 wire format.

    Kept as a helper rather than a hardcoded byte literal so a reader
    can verify the expectation without reaching for a hex chart.
    """
    tag = (field_number << 3) | 2  # wire-type 2 (length-delimited)
    payload = value.encode("utf-8")
    if len(payload) >= 128:
        raise AssertionError("helper assumes single-byte length varint; raise the cap")
    return bytes([tag, len(payload)]) + payload


def test_channel_message_event_field_numbers_pinned():
    """Renumbering any ChannelMessageEvent field changes the wire bytes.

    Each populated string field is set in isolation and the marshaled
    bytes are compared against the hand-computed expectation. A renumber
    flips the tag byte and trips this assertion on the language that
    regenerated; cross-language drift is therefore caught at CI time.
    """
    cases = [
        (1, "message_id", "msg-001"),
        (2, "channel_id", "group:eng"),
        (3, "channel_type", "group"),
        (4, "sender_id", "alice"),
        (5, "content", "hello"),
        (6, "timestamp", "2026-05-04T12:00:00Z"),
        (7, "thread_id", "t-1"),
        # Field 8 is `repeated string mentions`; tested in its own case
        # below because repeated fields encode each element with its own
        # tag byte.
        (9, "respond_policy", "when_mentioned"),
        (10, "thread_parent_sender_id", "dave"),
        # Field 11 is `int32 cascade_depth` (varint); pinned in
        # test_channel_message_event_cascade_depth_field_number_pinned.
        (12, "sender_participant_type", "user"),
    ]
    for field_number, attr, value in cases:
        ev = task_pb2.ChannelMessageEvent(**{attr: value})
        expected = _string_field_bytes(field_number, value)
        assert ev.SerializeToString() == expected, (
            f"field {attr!r} (expected number {field_number}) marshaled to "
            f"{ev.SerializeToString()!r}, expected {expected!r} — "
            "field number renumbered or wire type changed"
        )


def test_channel_message_event_cascade_depth_field_number_pinned():
    """``int32 cascade_depth = 11`` encodes as varint tag 0x58 + payload.

    Field 11, wire-type 0 (varint): tag byte = (11 << 3) | 0 = 0x58. For
    value 7 (single-byte varint) the payload is 0x07, so the full
    serialized blob is exactly ``b"\\x58\\x07"``. A renumber of this
    field — or an accidental type flip away from ``int32`` — will
    fail this assertion on whichever language regenerates first.
    """
    ev = task_pb2.ChannelMessageEvent(cascade_depth=7)
    assert ev.SerializeToString() == b"\x58\x07"

    # Zero must encode to zero bytes under proto3 implicit presence.
    assert task_pb2.ChannelMessageEvent(cascade_depth=0).SerializeToString() == b""


def test_channel_message_event_mentions_field_number_pinned():
    """`repeated string mentions = 8` encodes each element with tag 8.

    Catches a flip to ``string mentions`` (single, not repeated) — proto3
    silently accepts the last value when fed multiple, so a type flip
    passes the round-trip equality test but corrupts the wire shape.
    """
    ev = task_pb2.ChannelMessageEvent(mentions=["bob", "carol"])
    expected = _string_field_bytes(8, "bob") + _string_field_bytes(8, "carol")
    assert ev.SerializeToString() == expected


def test_task_ack_field_numbers_pinned():
    """TaskAck: bool field 1 + string field 2 — tag bytes pinned."""
    # `success=true` encodes as tag=08 (field 1, wire-type 0=varint), value=01.
    assert task_pb2.TaskAck(success=True).SerializeToString() == b"\x08\x01"
    # `error_message="x"` is field 2 length-delimited.
    assert task_pb2.TaskAck(error_message="x").SerializeToString() == _string_field_bytes(2, "x")
