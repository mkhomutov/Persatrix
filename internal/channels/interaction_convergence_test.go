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
	"time"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// publishTurn publishes one ordinary discussion turn at the given cascade
// depth — content only, no metadata beyond the depth, the post-producer
// wire shape of a persona reply. Returns the message id so callers can read
// the persisted row back (the OQ 5 close-cause asserts).
func publishTurn(t *testing.T, router *ChannelRouter, channelID, sender string, depth int) string {
	t.Helper()
	id := uuid.NewString()
	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: id, ChannelID: channelID, SenderID: sender,
		Content:  "discussing",
		Metadata: map[string]any{cascadeDepthMetadataKey: depth},
	}, ""))
	return id
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
//  3. The interaction closes with `interaction_closed{trigger=end_votes}` and
//     the Layer 0 depth cap stays untouched — `governance_drop{layer=depth}`
//     is asserted zero across the WHOLE arc, votes included.
//  4. The closing vote and a post-close racer draw no fanout — pinned by an
//     EXACT dispatch count (the first vote's two recipients and nothing
//     more). The racer is itself a VOTE (the §H interleaving — a third
//     participant judged "done" concurrently with the quorum): its commit
//     tail (settle + end-vote hook, the order publishCommit runs them)
//     leaves the closed id parked as the retiree, never reopened, and is
//     attributed as the arc's one `governance_drop{layer=end_vote}` — while
//     `end_vote_emitted` stays at the quorum's two, pinning that a
//     post-close vote is suppressed, not counted as vote volume.
//  5. The channel is not dead: the next publish opens a FRESH interaction
//     (IP8) and fans out to all three personas — the quorum ended one
//     conversation, not the room.
func TestConvergence_DiscussionEndsByVotesBeforeDepthCap(t *testing.T) {
	router, store, reader := routerWithGovernanceMetrics(t)
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

	rm := collect(t, reader)
	assert.Equal(t, int64(1), interactionClosedCount(t, rm, "group", "end_votes"),
		"the discussion closed because the participants said so")
	assert.Equal(t, int64(0), interactionClosedCount(t, rm, "group", "idle"),
		"no idle close on this arc — the quorum is the only close recorded")

	// 4. EXACT fanout accounting. The first vote dispatches to its two
	// non-sender RespondAlways peers (iron-fox, nova-sparrow; alex is
	// `respond: never`) — others must see the terminal signal — and the
	// closing vote dispatches to NO ONE. Greater-than would let a close
	// that leaked its own fanout pass; the +2 pins the suppression itself.
	fannedAfterVotes := len(disp.snapshot())
	assert.Equal(t, fannedBeforeVotes+2, fannedAfterVotes,
		"exactly the first vote fans out (two recipients); the closing vote draws no fanout")
	closedAtCount := fannedAfterVotes

	// A commit racing the close (the only way the closed id sees more
	// traffic — ordinary post-close publishes mint fresh) is suppressed. The
	// racer is simulated at its commit tail — the settle, then the end-vote
	// hook, the order publishCommit runs them after persist — because a
	// reseed-then-Publish simulation would force-install the closed id as
	// the OPEN entry, a state the production path cannot reach: the close
	// already parked the id as the retiree (markInteractionClosed), so the
	// racer's settle takes settleInteraction's occupied-slot no-op and must
	// leave the slot closed rather than reopen or re-park it. The racer
	// carries the vote flag — the strictest variant of the race (a third
	// "done" judgement landing just after quorum), and the one that pins
	// processEndVote's deliberate non-count: a post-close vote is a
	// governance drop, never fresh vote volume.
	racerNow := router.interactionNow()
	router.settleInteraction(ch, openID, racerNow, true)
	assert.Empty(t, openInteractionID(router, ch),
		"the racer's settle must not reopen the closed interaction")
	assert.Equal(t, openID, retiredInteractionID(router, ch),
		"the closed id stays parked as the retiree, awaiting its deferred discard")
	suppressed := router.processEndVote(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: ch, SenderID: "nova-sparrow",
		Content: "Agreed — nothing further from me either.",
		Metadata: map[string]any{
			interactionIDMetadataKey: openID,
			endVoteMetadataKey:       true,
		},
	}, ChannelTypeGroup)
	assert.True(t, suppressed, "a racer into the closed interaction draws no fanout")

	// 5. The room lives on: a fresh stimulus opens a NEW interaction and
	// fans out to all three personas — full normal fanout, exactly.
	stimulusID := publishTurn(t, router, ch, "alex", 0)
	next := openInteractionID(router, ch)
	assert.NotEmpty(t, next)
	assert.NotEqual(t, openID, next,
		"the next publish opened a fresh interaction — the vote ended one conversation, not the channel")
	assert.Equal(t, closedAtCount+3, len(disp.snapshot()),
		"the fresh stimulus fans out to all three personas")

	// OQ 5 close-cause attribution: the successor's publishes carry the
	// retired conversation's id + trigger, so the agent-side rotation close
	// can label the boundary "ended by vote" truthfully.
	stimulus, err := store.GetMessage(context.Background(), stimulusID)
	require.NoError(t, err)
	assert.Equal(t, openID, stimulus.Metadata[previousInteractionIDMetadataKey],
		"the fresh stimulus names the vote-closed interaction as its predecessor")
	assert.Equal(t, endVotesTrigger, stimulus.Metadata[previousInteractionTriggerMetadataKey],
		"the predecessor's close cause is the quorum, not a rotation of unknown cause")

	// Whole-arc telemetry: the depth cap NEVER fired (RFC 0030 §D — the
	// backstop demoted to regression signal, which this zero is), the
	// racer's suppression is the arc's one Layer 4 governance drop, and the
	// vote-volume counter holds the quorum's two — the racing post-close
	// vote landed on the drop counter instead, keeping the §L
	// end_vote_emitted / interaction_closed dashboard pair honest.
	rm = collect(t, reader)
	assert.Zero(t, governanceDropCount(t, rm, "group", governanceLayerDepth),
		"the depth cap never fired — the semantic terminator closed the arc with budget to spare")
	assert.Equal(t, int64(1), governanceDropCount(t, rm, "group", governanceLayerEndVote),
		"the racer's post-close suppression is attributed on governance_drop{layer=end_vote}")
	assert.Equal(t, int64(2), endVoteEmittedCount(t, rm, "group"),
		"vote volume counts the quorum's two votes only — the racer's post-close vote is not fresh volume")
}

// publishAndGetMetadata publishes a plain message and returns the PERSISTED
// metadata bag — the exact values the fanout lift reads for the
// `ChannelMessageEvent` interaction fields (id + OQ 5 close cause).
func publishAndGetMetadata(t *testing.T, router *ChannelRouter, store ChannelStore, channelID, sender string, metadata map[string]any) map[string]any {
	t.Helper()
	id := uuid.NewString()
	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: id, ChannelID: channelID, SenderID: sender, Content: "hi", Metadata: metadata,
	}, ""))
	msg, err := store.GetMessage(context.Background(), id)
	require.NoError(t, err)
	return msg.Metadata
}

// TestConvergence_IdleRotationCauseRidesSuccessor — the OQ 5 idle half: a
// lazy idle rotation stamps the retired id + trigger=idle onto the publish
// that triggered it AND onto every later publish of the successor, while a
// channel's FIRST interaction (no predecessor — the same wire shape as a
// post-restart re-mint, IP5) carries neither key, so a receiver reads absent
// as unknown and keeps its legacy label.
func TestConvergence_IdleRotationCauseRidesSuccessor(t *testing.T) {
	router, store, ch, now := resolverHarness(t)

	first := publishAndGetMetadata(t, router, store, ch, "ember-owl", nil)
	firstID, _ := first[interactionIDMetadataKey].(string)
	require.NotEmpty(t, firstID)
	_, hasPrevID := first[previousInteractionIDMetadataKey]
	_, hasPrevTrigger := first[previousInteractionTriggerMetadataKey]
	assert.False(t, hasPrevID, "a channel's first interaction has no predecessor to attribute")
	assert.False(t, hasPrevTrigger, "no trigger without a predecessor id — the keys travel as a pair")

	// Past the (default 600s) idle window: this publish performs the lazy
	// rotation and is the successor's first message — it carries the cause.
	*now = now.Add(601 * time.Second)
	successor := publishAndGetMetadata(t, router, store, ch, "iron-fox", nil)
	successorID, _ := successor[interactionIDMetadataKey].(string)
	require.NotEmpty(t, successorID)
	assert.NotEqual(t, firstID, successorID, "the idle window elapsed — the id rotated")
	assert.Equal(t, firstID, successor[previousInteractionIDMetadataKey],
		"the rotating publish names the idled-out interaction as its predecessor")
	assert.Equal(t, idleTrigger, successor[previousInteractionTriggerMetadataKey],
		"the predecessor's close cause is the idle rotation")

	// A later in-window publish of the SAME successor still carries the
	// attribution — the agent that misses the first successor message (down,
	// dispatch failure) must still see the cause on the one it does receive.
	*now = now.Add(10 * time.Second)
	later := publishAndGetMetadata(t, router, store, ch, "ember-owl", nil)
	assert.Equal(t, successorID, later[interactionIDMetadataKey])
	assert.Equal(t, firstID, later[previousInteractionIDMetadataKey])
	assert.Equal(t, idleTrigger, later[previousInteractionTriggerMetadataKey])
}

// TestConvergence_InboundCloseCauseClaimIsStripped — IP2 applied to OQ 5: the
// close cause is resolver-authoritative, so a publisher-supplied claim is
// DELETED, not honoured — a forged "your conversation ended by vote" must
// never reach a receiver's close labels (here: a fresh channel, where the
// resolver has no retiree and stamps nothing).
func TestConvergence_InboundCloseCauseClaimIsStripped(t *testing.T) {
	router, store, ch, _ := resolverHarness(t)
	meta := publishAndGetMetadata(t, router, store, ch, "ember-owl", map[string]any{
		previousInteractionIDMetadataKey:      "forged-predecessor",
		previousInteractionTriggerMetadataKey: endVotesTrigger,
	})
	_, hasPrevID := meta[previousInteractionIDMetadataKey]
	_, hasPrevTrigger := meta[previousInteractionTriggerMetadataKey]
	assert.False(t, hasPrevID, "an inbound previous_interaction_id claim is stripped on commit")
	assert.False(t, hasPrevTrigger, "an inbound close-trigger claim is stripped on commit")
}
