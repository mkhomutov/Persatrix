package channels

// Wire-side tests for the participant-type propagation half of
// ISSUE-0068 / the [RFC 0011 participant-type amendment]: the dispatcher
// must lift `msg.Metadata["participant_type"]` onto the typed
// `ChannelMessageEvent.sender_participant_type` proto field. Split from
// grpc_dispatcher_test.go to keep that file under the 500-line cap
// (`scripts/checks/file_size.py --strict`), mirroring the
// router_cascade_depth_test.go feature-split.
//
// [RFC 0011 participant-type amendment]: ../../docs/rfcs/0011-amendment-participant-type-wire-propagation.md

import (
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"go.uber.org/zap"
)

// TestChannelMessageToProto_ParticipantType_PropagatedFromMetadata pins
// the wire-side half of the amendment: the inbound `participant_type`
// carried on `msg.Metadata` (set by the REST chat handler) MUST land on
// the typed `string sender_participant_type` proto field. Without this,
// the peer type is dropped at the proto boundary and the agent defaults
// every channel-delivered chat peer to `agent` — ISSUE-0068.
func TestChannelMessageToProto_ParticipantType_PropagatedFromMetadata(t *testing.T) {
	d := &GRPCMessageDispatcher{logger: zap.NewNop()}
	ev := d.channelMessageToProto(ChannelMessage{
		ID: "m-1", ChannelID: "dm:agent-b:alice", SenderID: "alice",
		Content: "hi", Timestamp: time.Now().UTC(),
		Metadata: map[string]any{"participant_type": "user"},
	}, DispatchEnvelope{
		Recipient: Member{ParticipantID: "agent-b", RespondPolicy: RespondAlways},
	})

	assert.Equal(t, "user", ev.SenderParticipantType,
		"participant_type from msg.Metadata MUST land on the typed proto field")
}

// TestChannelMessageToProto_ParticipantType_DefaultsToEmpty pins that a
// channel message without a `participant_type` metadata entry (genuine
// agent-to-agent channel traffic) carries the proto3 empty default — the
// agent-side read path then resolves it to `agent`, the correct peer
// type for inter-agent messages. Only the REST chat handler injects the
// `user` value; ordinary channel fanout must not fabricate one.
func TestChannelMessageToProto_ParticipantType_DefaultsToEmpty(t *testing.T) {
	d := &GRPCMessageDispatcher{logger: zap.NewNop()}
	ev := d.channelMessageToProto(ChannelMessage{
		ID: "m-1", ChannelID: "group:planning", SenderID: "agent-a",
		Content: "hi", Timestamp: time.Now().UTC(),
	}, DispatchEnvelope{
		Recipient: Member{ParticipantID: "agent-b", RespondPolicy: RespondAlways},
	})

	assert.Empty(t, ev.SenderParticipantType,
		"absent metadata MUST translate to proto3 default empty sender_participant_type")
}

// TestChannelMessageToProto_ParticipantType_NonStringIgnored pins the
// defensive read: a malformed (non-string) `participant_type` metadata
// value falls back to empty rather than panicking on a bad type
// assertion — mirrors readCascadeDepth's tolerance of a malformed claim.
func TestChannelMessageToProto_ParticipantType_NonStringIgnored(t *testing.T) {
	d := &GRPCMessageDispatcher{logger: zap.NewNop()}
	ev := d.channelMessageToProto(ChannelMessage{
		ID: "m-1", ChannelID: "dm:agent-b:alice", SenderID: "alice",
		Content: "hi", Timestamp: time.Now().UTC(),
		Metadata: map[string]any{"participant_type": 42},
	}, DispatchEnvelope{
		Recipient: Member{ParticipantID: "agent-b", RespondPolicy: RespondAlways},
	})

	assert.Empty(t, ev.SenderParticipantType,
		"non-string participant_type metadata MUST fall back to empty")
}
