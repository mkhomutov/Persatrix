package channels

// Proto wire-shape regression tests for ChannelMessageEvent and
// TaskAck (ISSUE-0021, follow-up to PR #246).
//
// Every other test in this package exercises the dispatcher or the
// router with in-process Go structs; none of them serialize the
// generated proto type to bytes and read it back. A field-number
// renumber accident (e.g. swapping `content = 5` and `timestamp = 6`)
// or a type flip on a future proto edit would survive the existing
// tests because the Go-side handler tests construct the proto in-process.
//
// This file pins the wire shape on the orchestrator side; the mirror
// Python test lives at
// `tests/unit/python/test_channel_message_event_roundtrip.py`. Cross-
// language drift (Python emits a value, Go fails to parse it) is caught
// because both sides exercise the same field set against the same
// canonical proto.
//
// Located in `internal/channels/` rather than `internal/generated/taskpb/`
// because `make clean` recreates the generated dir from scratch via
// `rm -rf $(PROTO_GO_OUT)` — a test there would be wiped on every
// clean+regen cycle. The dispatcher package is the authoritative
// orchestrator-side consumer of these messages, so it is the natural
// home for the wire-shape pin.

import (
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"google.golang.org/protobuf/proto"

	"github.com/mkhomutov/persatrix/internal/generated/taskpb"
)

func TestChannelMessageEvent_RoundTripsAllFields(t *testing.T) {
	// Populate every field declared in proto/task.proto with a
	// non-default value so a missing field surfaces as a decoded value
	// reverting to proto3's zero (empty string / nil slice) and trips
	// the equality check below.
	original := &taskpb.ChannelMessageEvent{
		MessageId:            "msg-001",
		ChannelId:            "group:eng",
		ChannelType:          "group",
		SenderId:             "alice",
		Content:              "hello world",
		Timestamp:            "2026-05-04T12:00:00Z",
		ThreadId:             "t-1",
		Mentions:             []string{"bob", "carol"},
		RespondPolicy:        "when_mentioned",
		ThreadParentSenderId: "dave",
		CascadeDepth:         3,
	}

	blob, err := proto.Marshal(original)
	require.NoError(t, err)

	decoded := &taskpb.ChannelMessageEvent{}
	require.NoError(t, proto.Unmarshal(blob, decoded))

	// proto.Equal handles unknown-field tolerance correctly; a strict
	// reflect.DeepEqual would also work here but proto.Equal is the
	// idiomatic comparison for proto messages.
	assert.True(t, proto.Equal(original, decoded),
		"proto.Equal: got %+v want %+v", decoded, original)

	// Per-field reachability: equality alone catches a wholesale
	// rename only if both sides moved together. Naming each field here
	// pins the public Go accessor surface.
	assert.Equal(t, "msg-001", decoded.MessageId)
	assert.Equal(t, "group:eng", decoded.ChannelId)
	assert.Equal(t, "group", decoded.ChannelType)
	assert.Equal(t, "alice", decoded.SenderId)
	assert.Equal(t, "hello world", decoded.Content)
	assert.Equal(t, "2026-05-04T12:00:00Z", decoded.Timestamp)
	assert.Equal(t, "t-1", decoded.ThreadId)
	assert.Equal(t, []string{"bob", "carol"}, decoded.Mentions)
	assert.Equal(t, "when_mentioned", decoded.RespondPolicy)
	assert.Equal(t, "dave", decoded.ThreadParentSenderId)
	assert.Equal(t, int32(3), decoded.CascadeDepth)
}

// TestChannelMessageEvent_CascadeDepthRoundTripsWithoutValue pins the proto3
// implicit-presence contract for the new `cascade_depth` field: an event
// constructed without setting `CascadeDepth` must decode to the zero value
// and the on-wire payload must omit the field entirely. Catches an
// accidental `optional`-keyword promotion that would force explicit
// presence and change the marshaled bytes.
func TestChannelMessageEvent_CascadeDepthRoundTripsWithoutValue(t *testing.T) {
	original := &taskpb.ChannelMessageEvent{
		MessageId: "msg-002",
		ChannelId: "group:eng",
		// CascadeDepth deliberately omitted — proto3 zero.
	}
	blob, err := proto.Marshal(original)
	require.NoError(t, err)

	decoded := &taskpb.ChannelMessageEvent{}
	require.NoError(t, proto.Unmarshal(blob, decoded))
	assert.True(t, proto.Equal(original, decoded))
	assert.Equal(t, int32(0), decoded.CascadeDepth)
}

func TestChannelMessageEvent_DefaultInstanceRoundTrips(t *testing.T) {
	// A default-constructed event must round-trip to a zero-byte payload
	// and back. Pins the proto3 implicit-presence contract: an unset
	// field is indistinguishable from an explicitly-set zero. Catches
	// the accidental introduction of a `required`-shaped field (e.g. a
	// scalar marshalled with explicit presence via `optional` keyword
	// where a downstream consumer expected the zero default).
	original := &taskpb.ChannelMessageEvent{}
	blob, err := proto.Marshal(original)
	require.NoError(t, err)
	assert.Empty(t, blob, "proto3 message with all-zero fields must marshal to zero bytes")

	decoded := &taskpb.ChannelMessageEvent{}
	require.NoError(t, proto.Unmarshal(blob, decoded))
	assert.True(t, proto.Equal(original, decoded))
}

func TestTaskAck_RoundTripsBothFields(t *testing.T) {
	// TaskAck is the response of every ReceiveChannelMessage call. A
	// renumber accident on its two fields would silently strand
	// `error_message`, which the orchestrator-side dispatcher reads to
	// classify rejected dispatches.
	original := &taskpb.TaskAck{
		Success:      false,
		ErrorMessage: "invalid_channel_type",
	}
	blob, err := proto.Marshal(original)
	require.NoError(t, err)

	decoded := &taskpb.TaskAck{}
	require.NoError(t, proto.Unmarshal(blob, decoded))

	assert.True(t, proto.Equal(original, decoded))
	assert.False(t, decoded.Success)
	assert.Equal(t, "invalid_channel_type", decoded.ErrorMessage)
}

func TestTaskAck_SuccessTrueRoundTrips(t *testing.T) {
	// Mirrors the happy-path ack the dispatcher sends back on enqueue;
	// keeping `success=true` covered as a separate case ensures the
	// proto3 bool encoding (varint 1) survives Marshal/Unmarshal — a
	// silent accidental flip to int32 would compile but corrupt the
	// wire form.
	original := &taskpb.TaskAck{Success: true}
	blob, err := proto.Marshal(original)
	require.NoError(t, err)

	decoded := &taskpb.TaskAck{}
	require.NoError(t, proto.Unmarshal(blob, decoded))

	assert.True(t, proto.Equal(original, decoded))
	assert.True(t, decoded.Success)
	assert.Empty(t, decoded.ErrorMessage)
}

// ─── Golden-bytes pin: catches field-number renumber on this side ───
//
// The round-trip tests above use the SAME generated stub for Marshal
// and Unmarshal, so a symmetric proto edit (e.g. swapping `content = 5`
// and `timestamp = 6`) regenerates both ends together and the equality
// check passes — the actual class of bug ISSUE-0021 names ("cross-
// language drift: Python emits a value, Go fails to parse") is invisible
// without pinning the wire bytes themselves.
//
// These tests pin the encoding of one field at a time against a hand-
// computed proto3 wire form. Concretely, `string` field N encodes as:
//
//	tag = (N << 3) | 2          ; wire-type 2 = length-delimited
//	payload = varint(len) || utf8_bytes
//
// A renumber on EITHER side breaks these tests because the produced
// bytes no longer match the pinned constant. The Python mirror test
// in `tests/unit/python/test_channel_message_event_roundtrip.py`
// decodes the same constants — if either language renumbers without
// the other, the constant fails on the regenerating side first and
// CI catches it.

// stringFieldBytes hand-encodes `string field_number = ...` per proto3
// wire format. Kept as a helper rather than a hardcoded byte literal so
// a reader can verify the expectation without reaching for a hex chart.
func stringFieldBytes(t *testing.T, fieldNumber int, value string) []byte {
	t.Helper()
	tag := byte((fieldNumber << 3) | 2) // wire-type 2 (length-delimited)
	payload := []byte(value)
	require.Less(t, len(payload), 128, "helper assumes single-byte length varint; raise the cap")
	out := []byte{tag, byte(len(payload))}
	return append(out, payload...)
}

func TestChannelMessageEvent_FieldNumbersPinned(t *testing.T) {
	cases := []struct {
		fieldNumber int
		name        string
		set         func(*taskpb.ChannelMessageEvent, string)
		value       string
	}{
		{1, "message_id", func(e *taskpb.ChannelMessageEvent, v string) { e.MessageId = v }, "msg-001"},
		{2, "channel_id", func(e *taskpb.ChannelMessageEvent, v string) { e.ChannelId = v }, "group:eng"},
		{3, "channel_type", func(e *taskpb.ChannelMessageEvent, v string) { e.ChannelType = v }, "group"},
		{4, "sender_id", func(e *taskpb.ChannelMessageEvent, v string) { e.SenderId = v }, "alice"},
		{5, "content", func(e *taskpb.ChannelMessageEvent, v string) { e.Content = v }, "hello"},
		{6, "timestamp", func(e *taskpb.ChannelMessageEvent, v string) { e.Timestamp = v }, "2026-05-04T12:00:00Z"},
		{7, "thread_id", func(e *taskpb.ChannelMessageEvent, v string) { e.ThreadId = v }, "t-1"},
		// Field 8 is `repeated string mentions`; tested in its own case
		// below because repeated fields encode each element with its
		// own tag byte.
		{9, "respond_policy", func(e *taskpb.ChannelMessageEvent, v string) { e.RespondPolicy = v }, "when_mentioned"},
		{10, "thread_parent_sender_id", func(e *taskpb.ChannelMessageEvent, v string) { e.ThreadParentSenderId = v }, "dave"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			ev := &taskpb.ChannelMessageEvent{}
			tc.set(ev, tc.value)
			blob, err := proto.Marshal(ev)
			require.NoError(t, err)
			expected := stringFieldBytes(t, tc.fieldNumber, tc.value)
			assert.Equal(t, expected, blob,
				"field %q (expected number %d) marshaled to wrong bytes — field number renumbered or wire type changed",
				tc.name, tc.fieldNumber)
		})
	}
}

func TestChannelMessageEvent_CascadeDepthFieldNumberPinned(t *testing.T) {
	// `int32 cascade_depth = 11` is varint-encoded (wire-type 0):
	//   tag = (11 << 3) | 0 = 0x58
	//   value 7 encodes as varint 0x07 (single byte, payload < 0x80).
	// A renumber of this field — or an accidental type flip to a
	// length-delimited wire type — will fail this assertion on
	// whichever language regenerates first.
	ev := &taskpb.ChannelMessageEvent{CascadeDepth: 7}
	blob, err := proto.Marshal(ev)
	require.NoError(t, err)
	assert.Equal(t, []byte{0x58, 0x07}, blob,
		"cascade_depth field 11 must marshal as varint tag 0x58 + payload — field renumbered or wire type changed")

	// Zero must encode to nothing (proto3 implicit presence).
	blobZero, err := proto.Marshal(&taskpb.ChannelMessageEvent{CascadeDepth: 0})
	require.NoError(t, err)
	assert.Empty(t, blobZero, "cascade_depth=0 must marshal to zero bytes under proto3 implicit presence")
}

func TestChannelMessageEvent_MentionsFieldNumberPinned(t *testing.T) {
	// `repeated string mentions = 8` encodes each element with tag 8.
	// Catches a flip to `string mentions` (single, not repeated) —
	// proto3 silently accepts the last value when fed multiple, so a
	// type flip passes the round-trip equality test but corrupts the
	// wire shape.
	ev := &taskpb.ChannelMessageEvent{Mentions: []string{"bob", "carol"}}
	blob, err := proto.Marshal(ev)
	require.NoError(t, err)
	expected := append(
		stringFieldBytes(t, 8, "bob"),
		stringFieldBytes(t, 8, "carol")...,
	)
	assert.Equal(t, expected, blob)
}

func TestTaskAck_FieldNumbersPinned(t *testing.T) {
	// `success=true` encodes as tag=08 (field 1, wire-type 0=varint), value=01.
	blob, err := proto.Marshal(&taskpb.TaskAck{Success: true})
	require.NoError(t, err)
	assert.Equal(t, []byte{0x08, 0x01}, blob)

	// `error_message="x"` is field 2 length-delimited.
	blob, err = proto.Marshal(&taskpb.TaskAck{ErrorMessage: "x"})
	require.NoError(t, err)
	assert.Equal(t, stringFieldBytes(t, 2, "x"), blob)
}
