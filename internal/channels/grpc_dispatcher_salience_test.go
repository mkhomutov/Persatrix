package channels

// RFC 0030 Tier B (v0.3.8) PR 2b — the dispatcher-side half of the wire that
// flips the PR-2a-dormant salience seam live. Kept in its own file (sibling to
// grpc_dispatcher_{otel,epoch,session}_test.go) so grpc_dispatcher_test.go
// stays under the 500-line review cap.

import (
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
)

// TestChannelMessageToProto_PopulatesSalienceFields pins the wiring:
// channelMessageToProto carries the per-recipient bid signals off the
// recipient's membership row and the per-publish channel size + cap off the
// envelope. This is the line that flips the dormant agent-side seam live (PR
// 2a shipped the seam; nothing set its inputs until here).
func TestChannelMessageToProto_PopulatesSalienceFields(t *testing.T) {
	d := &GRPCMessageDispatcher{logger: zap.NewNop()}
	thr := 0.3

	// A salience-gated participant with an explicit threshold.
	ev := d.channelMessageToProto(
		ChannelMessage{ID: "m-1", ChannelID: "group:planning", SenderID: "a"},
		DispatchEnvelope{
			Recipient:                 Member{ParticipantID: "b", RespondPolicy: RespondAlways, SalienceGated: true, Threshold: &thr},
			ChannelSize:               4,
			SalienceMaxChannelMembers: 20,
		})
	assert.True(t, ev.SalienceGated, "a salience-gated recipient carries salience_gated=true")
	require.NotNil(t, ev.Threshold, "an explicit threshold is present on the wire")
	assert.Equal(t, 0.3, ev.GetThreshold())
	assert.Equal(t, int32(4), ev.ChannelSize)
	assert.Equal(t, int32(20), ev.SalienceMaxChannelMembers)

	// A legacy `always` recipient (not salience-gated, unset threshold) —
	// proves the back-compat default: false + absent threshold on the wire.
	ev = d.channelMessageToProto(
		ChannelMessage{ID: "m-2", ChannelID: "group:planning", SenderID: "a"},
		DispatchEnvelope{
			Recipient:   Member{ParticipantID: "c", RespondPolicy: RespondAlways},
			ChannelSize: 4,
		})
	assert.False(t, ev.SalienceGated, "a legacy always recipient is not salience-gated")
	assert.Nil(t, ev.Threshold, "an unset threshold is absent on the wire (distinct from 0.0)")
}
