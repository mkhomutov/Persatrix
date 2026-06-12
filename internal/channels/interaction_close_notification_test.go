package channels

// RFC 0030 end-vote-close-propagation amendment (CP1/CP2) — acceptance,
// landed with the amendment doc (PR 1) and skip-guarded until the
// close-notification dispatch exists. PR 2 of the workstream removes the
// skip and extends the envelope assertions to the typed
// `interaction_close_notification` marker once the proto field is
// regenerated (the marker cannot be referenced before it compiles).
//
// The contract under test: an `end_votes` close is PROPAGATED to the
// room — every dispatch-served non-sender member (RespondAlways /
// RespondWhenMentioned) receives the closing message at close time
// through the per-recipient dispatch seam — while ordinary fanout of
// the closing vote stays suppressed (§H's posture is unchanged; the
// notification is delivery of a fact, not an invitation to speak).
// `respond: never` members are OUT of scope by design: fanout's v0.3.0
// short-circuit and [DispatchEnvelope.Recipient]'s documented invariant
// both exclude them upstream of the dispatcher, they run no agent-local
// tracker to starve, and the human surface reads the persisted closing
// vote from the store on demand.

import (
	"context"
	"sync"
	"testing"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
)

// closeDispatchRecorder captures (envelope, message) pairs under one
// lock. envelopeRecorder drops the message half and
// messageRecordingDispatcher appends to two slices without a mutex —
// fanout dispatches concurrently, and CP1's assertion needs the pair
// (WHO was notified and WHAT they were handed) race-free.
type closeDispatchRecorder struct {
	mu    sync.Mutex
	calls []closeDispatchCall
}

type closeDispatchCall struct {
	env DispatchEnvelope
	msg ChannelMessage
}

func (d *closeDispatchRecorder) Dispatch(_ context.Context, env DispatchEnvelope, msg ChannelMessage) error {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.calls = append(d.calls, closeDispatchCall{env: env, msg: msg})
	return nil
}

func (d *closeDispatchRecorder) snapshot() []closeDispatchCall {
	d.mu.Lock()
	defer d.mu.Unlock()
	out := make([]closeDispatchCall, len(d.calls))
	copy(out, d.calls)
	return out
}

func TestEndVoteClose_NotifiesEveryMemberOfTheClose(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	disp := &closeDispatchRecorder{}
	router := NewChannelRouter(store, disp, zap.NewNop(), nil)
	ch := mustCreateGroupWithPolicies(t, store, "planning",
		map[string]RespondPolicy{
			"alex":         RespondNever, // the human — reads history on demand, never dispatched
			"ember-owl":    RespondAlways,
			"iron-fox":     RespondAlways,
			"nova-sparrow": RespondAlways,
		}, "alex", "ember-owl", "iron-fox", "nova-sparrow")

	// A short discussion, then the quorum: nova-sparrow proposes in its
	// vote, iron-fox concurs — the second distinct vote closes.
	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: ch, SenderID: "alex",
		Content: "relay or beacon — final call?",
	}, ""))
	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: ch, SenderID: "nova-sparrow",
		Content: "Synthesis: relay. Voting to close.",
		Metadata: map[string]any{
			cascadeDepthMetadataKey: 1,
			endVoteMetadataKey:      true,
		},
	}, ""))

	beforeClose := len(disp.snapshot())
	closingID := uuid.NewString()
	const closingContent = "Agreed — relay. Nothing further."
	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: closingID, ChannelID: ch, SenderID: "iron-fox",
		Content: closingContent,
		Metadata: map[string]any{
			cascadeDepthMetadataKey: 2,
			endVoteMetadataKey:      true,
		},
	}, ""))

	// CP5 makes the notification dispatch fire-and-forget, so nothing on
	// the publish path joins it — ordinary fanout (whose workers the sync
	// path DOES join) is suppressed for this very message. The router's
	// drain WaitGroup is the documented deterministic assert point
	// ([ChannelRouter.WaitForPendingFanout]); CP5 pins PR 2's dispatch
	// goroutines onto it, and the call is a no-op today, so the
	// red-without-skip posture is unchanged.
	router.WaitForPendingFanout()

	// CP1: every dispatch-served non-sender member heard about the close —
	// exactly once each — and what they were handed IS the closing vote
	// (the synthesis/concurrence is real history, not a digest), under a
	// fresh per-recipient event id (CP2, the CE3 dedup lesson). iron-fox
	// closed its own tracker by voting and is excluded; alex sits outside
	// the dispatch contract (see the header note).
	notified := map[string]int{}
	seenIDs := map[string]bool{}
	for _, call := range disp.snapshot()[beforeClose:] {
		notified[call.env.Recipient.ParticipantID]++
		assert.True(t, call.env.InteractionCloseNotification,
			"CP2: the dispatch carries the typed close-notification marker")
		assert.False(t, call.env.ChairEscalation,
			"the notification is not a forced turn — the markers never alias")
		assert.Equal(t, closingContent, call.msg.Content,
			"the notification carries the closing vote verbatim")
		assert.Equal(t, "iron-fox", call.msg.SenderID,
			"the closing message keeps its real author")
		assert.NotEqual(t, closingID, call.msg.ID,
			"CP2: each notification rides a fresh event id, not the persisted vote's")
		assert.False(t, seenIDs[call.msg.ID],
			"CP2: event ids are fresh PER RECIPIENT")
		seenIDs[call.msg.ID] = true
	}
	// The exactly-once shape carries CP2's suppression half as well:
	// ordinary fanout of the closing vote stays suppressed (a count of 2
	// for any member would mean the close un-suppressed fanout instead of
	// notifying).
	assert.Equal(t, map[string]int{
		"ember-owl":    1,
		"nova-sparrow": 1,
	}, notified,
		"the end_votes close reached every dispatch-served non-sender member exactly once")
	// PR 2 extends the loop's assertions to the typed
	// `interaction_close_notification` envelope marker once the proto
	// field compiles.
}
