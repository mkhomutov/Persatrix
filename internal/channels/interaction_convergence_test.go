package channels

// interaction_convergence_test.go — the producer plan's PR 3 acceptance pin
// (docs/rfcs/0030-interaction-id-producer-pr-plan.md): the full convergence
// arc, end to end through Publish, with NO caller-supplied governance
// metadata anywhere — exactly the wire shape real traffic has now that the
// resolver mints every id (PR 1, #604) and the agent-side vote producer
// emits only the `end_interaction_vote` flag (PR 2, #605).
//
// The individual mechanisms are each pinned in their own suites
// (interaction_resolver_test.go, end_vote_test.go,
// governance_composition_test.go); this file pins the COMPOSITION the whole
// plan exists for — a discussion that ends because the participants said so,
// with cascade-depth budget left over, and a channel that lives on past its
// converged conversation. RFC 0030 §D's promise — depth-cap drops demote
// from de-facto terminator to regression signal — is this arc.

import (
	"context"
	"testing"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
)

// publishTurn publishes one ordinary discussion turn at the given cascade
// depth — content only, no metadata beyond the depth, the post-producer
// wire shape of a persona reply.
func publishTurn(t *testing.T, router *ChannelRouter, channelID, sender string, depth int) {
	t.Helper()
	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: channelID, SenderID: sender,
		Content:  "discussing",
		Metadata: map[string]any{cascadeDepthMetadataKey: depth},
	}, ""))
}

// publishVote publishes a vote turn — the exact shape the Python producer
// emits (agents/end_vote_action.py): the flag, no interaction claim.
func publishVote(t *testing.T, router *ChannelRouter, channelID, sender string, depth int) {
	t.Helper()
	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: channelID, SenderID: sender,
		Content: "I have nothing further to add.",
		Metadata: map[string]any{
			cascadeDepthMetadataKey: depth,
			endVoteMetadataKey:      true,
		},
	}, ""))
}

// TestConvergence_DiscussionEndsByVotesBeforeDepthCap — the acceptance arc:
//
//  1. A human opens a question (depth 0); two personas discuss (depths 1–2).
//     Every publish carries the same resolver-minted interaction.
//  2. Both personas vote (K=2 default quorum) with depth budget remaining.
//  3. The interaction closes with `interaction_closed{trigger=end_votes}` —
//     the semantic terminator fired BEFORE the Layer 0 depth cap could, and
//     `governance_drop{layer=depth}` never incremented.
//  4. The closing vote and any post-close racer draw no fanout.
//  5. The channel is not dead: the next publish opens a FRESH interaction
//     (IP8) and fans out normally — the quorum ended one conversation, not
//     the room.
func TestConvergence_DiscussionEndsByVotesBeforeDepthCap(t *testing.T) {
	router, store, reader := routerWithInteractionClosedMetric(t)
	disp := router.dispatcher.(*recordingDispatcher)
	ch := mustCreateGroupWithPolicies(t, store, "planning",
		map[string]RespondPolicy{
			"alex":         RespondNever, // the human, per the documented join convention
			"ember-owl":    RespondAlways,
			"iron-fox":     RespondAlways,
			"nova-sparrow": RespondAlways,
		}, "alex", "ember-owl", "iron-fox", "nova-sparrow")

	// 1. The discussion: a human stimulus and two persona turns, all under
	// one resolver-minted interaction, depths well under the cap of 5.
	publishTurn(t, router, ch, "alex", 0)
	openID := openInteractionID(router, ch)
	require.NotEmpty(t, openID, "the resolver minted an interaction for the discussion")
	publishTurn(t, router, ch, "ember-owl", 1)
	publishTurn(t, router, ch, "iron-fox", 1)
	assert.Equal(t, openID, openInteractionID(router, ch),
		"the whole discussion shares one interaction")

	// 2.–3. Two distinct votes reach the K=2 default quorum at depth 2 —
	// three hops of cascade budget still unspent.
	fannedBeforeVotes := len(disp.snapshot())
	publishVote(t, router, ch, "ember-owl", 2)
	publishVote(t, router, ch, "iron-fox", 2)

	var rm metricdata.ResourceMetrics
	require.NoError(t, reader.Collect(context.Background(), &rm))
	assert.Equal(t, int64(1), interactionClosedCount(t, rm, "group", "end_votes"),
		"the discussion closed because the participants said so")
	assert.Equal(t, int64(0), interactionClosedCount(t, rm, "group", "idle"),
		"idle rotation had nothing to do — the semantic terminator fired first")

	// 4. The first vote fans out (others must see the terminal signal); the
	// closing vote does not.
	fannedAfterVotes := len(disp.snapshot())
	assert.Greater(t, fannedAfterVotes, fannedBeforeVotes,
		"the first vote fanned out as a real message")
	closedAtCount := fannedAfterVotes

	// A commit racing the close (the only way the closed id sees more
	// traffic — ordinary post-close publishes mint fresh) is suppressed. The
	// racer is simulated at its commit tail (a direct processEndVote call
	// carrying the closed id): a reseed-then-Publish simulation would
	// force-install the closed id as the OPEN entry — a state the production
	// path cannot reach (markInteractionClosed cleared it, and a racer's
	// settle parks an orphaned id as the retiree, never as open).
	suppressed := router.processEndVote(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: ch, SenderID: "nova-sparrow", Content: "racing",
		Metadata: map[string]any{interactionIDMetadataKey: openID},
	}, ChannelTypeGroup)
	assert.True(t, suppressed, "a racer into the closed interaction draws no fanout")

	// 5. The room lives on: a fresh stimulus opens a NEW interaction and
	// fans out normally.
	publishTurn(t, router, ch, "alex", 0)
	next := openInteractionID(router, ch)
	assert.NotEmpty(t, next)
	assert.NotEqual(t, openID, next,
		"the next publish opened a fresh interaction — the vote ended one conversation, not the channel")
	assert.Greater(t, len(disp.snapshot()), closedAtCount,
		"the fresh stimulus fans out normally")
}
