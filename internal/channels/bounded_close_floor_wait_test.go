package channels

// bounded_close_floor_wait_test.go — the deep-review follow-up pin for the
// floor-path close-before-dispatch hole. The fanout takes its terminating-state
// verdict ([stimulusOutlivedClose]) at the fanout HEAD, but the floor round then
// parks on the per-channel floor queue ([floorRegistry.acquire]) for up to
// N×turnTimeout while a prior round holds the floor — long enough for a sibling
// fanout's bounded close or armed synthesis turn to land. Before the fix the
// parked round dispatched a full multi-speaker LLM round into that just-closed
// discussion; [ChannelRouter.floorRound] now re-checks the verdict AFTER the wait
// and abandons, reporting the head-stale shape to the caller.

import (
	"context"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
)

// TestFloorRound_AbandonsWhenInteractionClosedDuringFloorWait — plant the
// interleaving the floor wait exposes: an autonomous interaction is deliberately
// closed (its id in the no-reopen ledger, the sibling-close shape) while a floor
// round is stamped for it. Calling floorRound directly stands in for the parked
// round waking to acquire the floor after that close. It must dispatch NO speaker
// and report abandoned=true, so the caller withholds instead of re-fanning LLM
// turns into the terminated discussion.
func TestFloorRound_AbandonsWhenInteractionClosedDuringFloorWait(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	disp := &messageRecordingDispatcher{}
	router := NewChannelRouter(store, disp, zap.NewNop(), nil)
	ch := mustCreateGroupWithPolicies(t, store, "planning",
		map[string]RespondPolicy{
			"nova-sparrow": RespondAlways,
			"ember-owl":    RespondAlways,
			"iron-fox":     RespondAlways,
		}, "nova-sparrow", "ember-owl", "iron-fox")
	router.SetAutonomous(ch, AutonomousConfig{Enabled: true, Convener: "nova-sparrow"})

	// Open + commit an interaction so the resolver entry exists, then deliberately
	// close it — the state a sibling fanout's bounded close leaves behind.
	interactionID, _, commit, _ := router.resolveInteractionID(context.Background(), ch, ChannelTypeGroup, "")
	commit(true)
	router.markInteractionClosed(ch, interactionID, structuralTrigger)

	// The parked round's stimulus, stamped for the now-closed interaction.
	msg := ChannelMessage{
		ID: uuid.NewString(), ChannelID: ch, SenderID: "operator", Content: "parked behind a bounding round",
		Metadata: map[string]any{interactionIDMetadataKey: interactionID},
	}
	responders := []Member{
		{ParticipantID: "ember-owl", RespondPolicy: RespondAlways},
		{ParticipantID: "iron-fox", RespondPolicy: RespondAlways},
	}
	a := router.AutonomousFor(ch)

	outcome, _, abandoned := router.floorRound(
		context.Background(), msg, ChannelTypeGroup, "",
		responders, nil /*nonResponders*/, time.Second, len(responders)+1, nil /*floorMentions*/, a,
	)

	assert.True(t, abandoned,
		"a floor round that acquires the floor after its interaction closed must abandon, not dispatch into the terminated discussion")
	assert.Equal(t, floorRoundOutcome{}, outcome, "an abandoned round reports the zero outcome — no turn ran")
	assert.Empty(t, disp.messages,
		"no speaker is dispatched once the discussion has terminated — the whole point of the post-wait re-check")
}

// TestFloorRound_RunsWhenInteractionStillOpen — the guard rail: the post-wait
// re-check must fire ONLY on a genuine termination. An open, un-closed
// interaction still runs its full round, so the fix cannot silently strangle a
// live floor round.
func TestFloorRound_RunsWhenInteractionStillOpen(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	disp := &messageRecordingDispatcher{}
	router := NewChannelRouter(store, disp, zap.NewNop(), nil)
	ch := mustCreateGroupWithPolicies(t, store, "planning",
		map[string]RespondPolicy{
			"nova-sparrow": RespondAlways,
			"ember-owl":    RespondAlways,
		}, "nova-sparrow", "ember-owl")
	router.SetAutonomous(ch, AutonomousConfig{Enabled: true, Convener: "nova-sparrow"})

	interactionID, _, commit, _ := router.resolveInteractionID(context.Background(), ch, ChannelTypeGroup, "")
	commit(true) // open and committed — NOT closed

	msg := ChannelMessage{
		ID: uuid.NewString(), ChannelID: ch, SenderID: "operator", Content: "live round",
		Metadata: map[string]any{interactionIDMetadataKey: interactionID},
	}
	responders := []Member{{ParticipantID: "ember-owl", RespondPolicy: RespondAlways}}
	a := router.AutonomousFor(ch)

	_, _, abandoned := router.floorRound(
		context.Background(), msg, ChannelTypeGroup, "",
		responders, nil, 50*time.Millisecond, len(responders)+1, nil, a,
	)

	assert.False(t, abandoned, "a live interaction's floor round must not abandon")
	require.Len(t, disp.envelopes, 1, "the single responder is dispatched on a live round")
	assert.Equal(t, "ember-owl", disp.envelopes[0].Recipient.ParticipantID,
		"the stimulus is delivered to the floor speaker for its turn")
}
