package channels

// RFC 0030 interaction-id producer plan OQ 5 — the dispatcher-side lift of
// the retired-interaction close cause onto the typed proto fields. Sibling of
// grpc_dispatcher_salience_test.go (own file so grpc_dispatcher_test.go stays
// under the 500-line review cap).

import (
	"testing"

	"github.com/stretchr/testify/assert"
	"go.uber.org/zap"
)

// TestChannelMessageToProto_LiftsPreviousInteractionCloseCause pins the OQ 5
// wire lift: the publish-side `previous_interaction_id` /
// `previous_interaction_close_trigger` metadata (stamped by publishCommit
// from the resolver's close record) rides onto the typed proto fields, and
// the trigger reader is allowlisted to the §L vocabulary — an unrecognised
// value lifts as empty so the receiver keeps its legacy label rather than
// persisting a junk close reason.
func TestChannelMessageToProto_LiftsPreviousInteractionCloseCause(t *testing.T) {
	d := &GRPCMessageDispatcher{logger: zap.NewNop()}

	ev := d.channelMessageToProto(ChannelMessage{
		ID: "m-1", ChannelID: "group:planning", SenderID: "a",
		Metadata: map[string]any{
			interactionIDMetadataKey:              "int-B",
			previousInteractionIDMetadataKey:      "int-A",
			previousInteractionTriggerMetadataKey: idleTrigger,
		},
	}, DispatchEnvelope{Recipient: Member{ParticipantID: "b", RespondPolicy: RespondAlways}})
	assert.Equal(t, "int-A", ev.PreviousInteractionId)
	assert.Equal(t, idleTrigger, ev.PreviousInteractionCloseTrigger)

	// The end-vote trigger is the other allowlisted value.
	ev = d.channelMessageToProto(ChannelMessage{
		ID: "m-2", ChannelID: "group:planning", SenderID: "a",
		Metadata: map[string]any{
			previousInteractionIDMetadataKey:      "int-A",
			previousInteractionTriggerMetadataKey: endVotesTrigger,
		},
	}, DispatchEnvelope{Recipient: Member{ParticipantID: "b", RespondPolicy: RespondAlways}})
	assert.Equal(t, endVotesTrigger, ev.PreviousInteractionCloseTrigger)

	// Unrecognised trigger → the whole PAIR lifts as empty. proto field 21
	// is documented "Set iff `= 20` is set", and the receiver-side seed
	// point applies the pair only as a validated unit — a half pair (id
	// without trigger) would put a wire shape on the fanout the producer
	// contract says cannot exist (PR 607 second-pass review).
	ev = d.channelMessageToProto(ChannelMessage{
		ID: "m-3", ChannelID: "group:planning", SenderID: "a",
		Metadata: map[string]any{
			previousInteractionIDMetadataKey:      "int-A",
			previousInteractionTriggerMetadataKey: "cosmic-rays",
		},
	}, DispatchEnvelope{Recipient: Member{ParticipantID: "b", RespondPolicy: RespondAlways}})
	assert.Empty(t, ev.PreviousInteractionCloseTrigger,
		"an out-of-vocabulary trigger lifts as empty (legacy-label degradation)")
	assert.Empty(t, ev.PreviousInteractionId,
		"the id must not ride the wire alone — fields 20/21 are a pair")

	// Oversized id → the pair lifts as empty too (the other half-pair
	// direction): a trigger with no id attributes nothing.
	ev = d.channelMessageToProto(ChannelMessage{
		ID: "m-4", ChannelID: "group:planning", SenderID: "a",
		Metadata: map[string]any{
			previousInteractionIDMetadataKey:      string(make([]byte, interactionIDMaxBytes+1)),
			previousInteractionTriggerMetadataKey: idleTrigger,
		},
	}, DispatchEnvelope{Recipient: Member{ParticipantID: "b", RespondPolicy: RespondAlways}})
	assert.Empty(t, ev.PreviousInteractionId)
	assert.Empty(t, ev.PreviousInteractionCloseTrigger,
		"a trigger must not ride the wire without the id it attributes")

	ev = d.channelMessageToProto(ChannelMessage{
		ID: "m-5", ChannelID: "group:planning", SenderID: "a",
	}, DispatchEnvelope{Recipient: Member{ParticipantID: "b", RespondPolicy: RespondAlways}})
	assert.Empty(t, ev.PreviousInteractionId)
	assert.Empty(t, ev.PreviousInteractionCloseTrigger)
}
