package channels

import (
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"go.uber.org/zap"
)

// RFC 0030 deterministic governance layers (v0.3.8), PR 1 — the
// `interaction_id` wire-propagation substrate. These pin the orchestrator
// half of the contract (metadata → typed proto field); the agent half (proto
// field → event metadata) is pinned in the Python servicer test
// `tests/unit/python/test_receive_channel_message.py`. Kept in a dedicated
// file (mirroring `interaction_id.go`) so `grpc_dispatcher_test.go` stays
// under the 500-line review cap.

// TestChannelMessageToProto_InteractionID_PropagatedFromMetadata pins the
// substrate: the orchestrator lifts the publish metadata `interaction_id`
// onto the typed proto field so Layers 1/2/4 can attribute spend / count
// replies / accumulate end-votes per interaction.
func TestChannelMessageToProto_InteractionID_PropagatedFromMetadata(t *testing.T) {
	d := &GRPCMessageDispatcher{logger: zap.NewNop()}
	ev := d.channelMessageToProto(ChannelMessage{
		ID: "m-1", ChannelID: "group:planning", SenderID: "agent-a",
		Content: "hi", Timestamp: time.Now().UTC(),
		Metadata: map[string]any{"interaction_id": "4e2b7c9a-1f3d-4a6b-8c2e-9d0f1a2b3c4d"},
	}, DispatchEnvelope{
		Recipient: Member{ParticipantID: "agent-b", RespondPolicy: RespondAlways},
	})

	assert.Equal(t, "4e2b7c9a-1f3d-4a6b-8c2e-9d0f1a2b3c4d", ev.InteractionId,
		"interaction_id from msg.Metadata MUST land on the typed proto field")
}

// TestChannelMessageToProto_InteractionID_DefaultsToEmpty pins the proto3
// implicit-presence default: a publish with no interaction_id in metadata
// carries the empty string — the untracked case that leaves every
// governance layer at its uncapped default (the feature is additive).
func TestChannelMessageToProto_InteractionID_DefaultsToEmpty(t *testing.T) {
	d := &GRPCMessageDispatcher{logger: zap.NewNop()}
	ev := d.channelMessageToProto(ChannelMessage{
		ID: "m-1", ChannelID: "group:planning", SenderID: "agent-a",
		Content: "hi", Timestamp: time.Now().UTC(),
	}, DispatchEnvelope{
		Recipient: Member{ParticipantID: "agent-b", RespondPolicy: RespondAlways},
	})

	assert.Empty(t, ev.InteractionId,
		"absent metadata MUST translate to the proto3 default interaction_id=\"\"")
}

// TestChannelMessageToProto_InteractionID_IgnoresNonString pins the
// defensive read: a malformed (non-string) metadata claim must not poison
// the publish path — it falls back to empty (untracked), mirroring
// readCascadeDepth / readParticipantType tolerance.
func TestChannelMessageToProto_InteractionID_IgnoresNonString(t *testing.T) {
	d := &GRPCMessageDispatcher{logger: zap.NewNop()}
	ev := d.channelMessageToProto(ChannelMessage{
		ID: "m-1", ChannelID: "group:planning", SenderID: "agent-a",
		Content: "hi", Timestamp: time.Now().UTC(),
		Metadata: map[string]any{"interaction_id": 1234},
	}, DispatchEnvelope{
		Recipient: Member{ParticipantID: "agent-b", RespondPolicy: RespondAlways},
	})

	assert.Empty(t, ev.InteractionId,
		"non-string interaction_id metadata MUST fall back to empty, not panic")
}
