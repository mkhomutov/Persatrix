package channels

// RFC 0030 end-vote-close-propagation amendment (CP1/CP2) — acceptance,
// landed with the amendment doc (PR 1) and skip-guarded until the
// close-notification dispatch exists. PR 2 of the workstream removes the
// skip and extends the dispatch assertions to the typed
// `interaction_close_notification` envelope marker once the proto field
// is regenerated (the marker cannot be referenced before it compiles).
//
// The contract under test: an `end_votes` close is PROPAGATED to the
// room — every non-sender member receives the closing message at close
// time through the per-recipient dispatch seam — while ordinary fanout
// of the closing vote stays suppressed (§H's posture is unchanged; the
// notification is delivery of a fact, not an invitation to speak).

import (
	"context"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
)

func TestEndVoteClose_NotifiesEveryMemberOfTheClose(t *testing.T) {
	t.Skip("CP acceptance (0030-amendment-end-vote-close-propagation §E) — unskip in PR 2, the close-notification dispatch")

	store := newTestStore(t, SQLiteOptions{})
	disp := &envelopeRecorder{}
	router := NewChannelRouter(store, disp, zap.NewNop(), nil)
	ch := mustCreateGroupWithPolicies(t, store, "planning",
		map[string]RespondPolicy{
			"alex":         RespondNever, // the human
			"ember-owl":    RespondAlways,
			"iron-fox":     RespondAlways,
			"nova-sparrow": RespondAlways,
		}, "alex", "ember-owl", "iron-fox", "nova-sparrow")
	router.SetFloorControl(ch, true, time.Millisecond)

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
	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: closingID, ChannelID: ch, SenderID: "iron-fox",
		Content: "Agreed — relay. Nothing further.",
		Metadata: map[string]any{
			cascadeDepthMetadataKey: 2,
			endVoteMetadataKey:      true,
		},
	}, ""))

	// CP1: every non-sender member heard about the close — exactly once
	// each. alex (RespondNever, the human seam), ember-owl, and
	// nova-sparrow each get the closing message; iron-fox closed its own
	// tracker by voting and is excluded.
	notified := map[string]int{}
	for _, env := range disp.snapshot()[beforeClose:] {
		notified[env.Recipient.ParticipantID]++
	}
	// The exactly-once shape carries CP2 as well: ordinary fanout of the
	// closing vote stays suppressed (a count of 2 for any member would
	// mean the close un-suppressed fanout instead of notifying).
	assert.Equal(t, map[string]int{
		"alex":         1,
		"ember-owl":    1,
		"nova-sparrow": 1,
	}, notified,
		"the end_votes close reached every non-sender member exactly once")
	_ = closingID // PR 2 extends the assertion: the dispatched message IS the closing vote
}
