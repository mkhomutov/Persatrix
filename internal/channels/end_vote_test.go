package channels

import (
	"context"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
	"go.uber.org/zap"
	"go.uber.org/zap/zaptest/observer"
)

// RFC 0030 Layer 4 (v0.3.8) — end-of-interaction signal. When K distinct
// participants emit an end-vote within W consecutive turns the interaction
// closes on its own: fanout of the closing publish is suppressed and the
// per-interaction governance counters are discarded (§H). The layer is
// inert in production (no producer writes the vote flag yet); these tests
// drive the accumulator directly through the publish metadata bag.

// endVote drives a single publish carrying the Layer 4 end-vote flag for the
// given (sender, interaction) pair. A fresh UUID keeps per-message identity unique.
func endVote(t *testing.T, router *ChannelRouter, channelID, sender, interactionID string) error {
	t.Helper()
	meta := map[string]any{endVoteMetadataKey: true}
	if interactionID != "" {
		meta[interactionIDMetadataKey] = interactionID
	}
	return router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: channelID, SenderID: sender, Content: "I'm done", Metadata: meta,
	}, "")
}

// plainTurn drives a single non-vote publish carrying only the interaction_id,
// so it advances the per-interaction turn counter without casting a vote.
func plainTurn(t *testing.T, router *ChannelRouter, channelID, sender, interactionID string) error {
	t.Helper()
	meta := map[string]any{}
	if interactionID != "" {
		meta[interactionIDMetadataKey] = interactionID
	}
	return router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: channelID, SenderID: sender, Content: "more", Metadata: meta,
	}, "")
}

// routerWithInteractionClosedMetric builds a router whose InteractionClosed
// counter is collectible through a manual reader, mirroring the reply-budget
// governance-drop telemetry harness.
func routerWithInteractionClosedMetric(t *testing.T) (*ChannelRouter, ChannelStore, *sdkmetric.ManualReader) {
	t.Helper()
	reader := sdkmetric.NewManualReader()
	mp := sdkmetric.NewMeterProvider(sdkmetric.WithReader(reader))
	t.Cleanup(func() { _ = mp.Shutdown(context.Background()) })
	ctr, err := mp.Meter("test").Int64Counter("channel.conversation.interaction_closed")
	require.NoError(t, err)
	store := newTestStore(t, SQLiteOptions{})
	router := NewChannelRouter(store, &recordingDispatcher{}, zap.NewNop(), &RouterMetrics{InteractionClosed: ctr})
	return router, store, reader
}

// interactionClosedCount returns the interaction_closed counter value for the
// given channel_type + trigger attribute pair, or 0 if no matching point exists.
func interactionClosedCount(t *testing.T, rm metricdata.ResourceMetrics, channelType, trigger string) int64 {
	t.Helper()
	for _, sm := range rm.ScopeMetrics {
		for _, m := range sm.Metrics {
			if m.Name != "channel.conversation.interaction_closed" {
				continue
			}
			sum, ok := m.Data.(metricdata.Sum[int64])
			require.Truef(t, ok, "interaction_closed: expected Sum[int64], got %T", m.Data)
			for _, dp := range sum.DataPoints {
				ct, _ := dp.Attributes.Value("channel_type")
				tg, _ := dp.Attributes.Value("trigger")
				if ct.AsString() == channelType && tg.AsString() == trigger {
					return dp.Value
				}
			}
		}
	}
	return 0
}

// TestEndVote_KDistinctVotesWithinWindowCloses pins the core Layer 4 contract
// (§H): K=2 distinct participants voting within W=3 consecutive turns closes
// the interaction with interaction_closed{trigger=end_votes}, and the closing
// publish's fanout is suppressed (no new replies are dispatched).
func TestEndVote_KDistinctVotesWithinWindowCloses(t *testing.T) {
	router, store, reader := routerWithInteractionClosedMetric(t)
	disp := router.dispatcher.(*recordingDispatcher)
	id := mustCreateGroup(t, store, "planning", "alice", "bob", "carol")
	router.SetEndVoteParams(id, 2, 3)

	// First vote: not enough for K=2.
	require.NoError(t, endVote(t, router, id, "alice", "int-1"))
	before := len(disp.snapshot())

	// Second distinct vote within the window closes the interaction.
	require.NoError(t, endVote(t, router, id, "bob", "int-1"))

	var rm metricdata.ResourceMetrics
	require.NoError(t, reader.Collect(context.Background(), &rm))
	assert.Equal(t, int64(1), interactionClosedCount(t, rm, "group", "end_votes"),
		"K distinct votes within W closes the interaction exactly once")

	assert.Equal(t, before, len(disp.snapshot()),
		"the closing vote's fanout is suppressed — no new replies dispatched")
}

// TestEndVote_DoubleVoteDedupes pins the per-(participant, interaction) dedupe
// (§H vote tampering): one participant voting twice counts once, so it cannot
// close a K=2 interaction alone.
func TestEndVote_DoubleVoteDedupes(t *testing.T) {
	router, store, reader := routerWithInteractionClosedMetric(t)
	id := mustCreateGroup(t, store, "planning", "alice", "bob")
	router.SetEndVoteParams(id, 2, 3)

	require.NoError(t, endVote(t, router, id, "alice", "int-1"))
	require.NoError(t, endVote(t, router, id, "alice", "int-1"))

	var rm metricdata.ResourceMetrics
	require.NoError(t, reader.Collect(context.Background(), &rm))
	assert.Equal(t, int64(0), interactionClosedCount(t, rm, "group", "end_votes"),
		"a single participant voting twice must not close a K=2 interaction")
}

// TestEndVote_OutOfWindowVotesDoNotAccumulate pins that an old vote goes stale:
// with W=3, a vote separated from a second vote by W or more turns no longer
// counts toward the quorum ("votes must be recent").
func TestEndVote_OutOfWindowVotesDoNotAccumulate(t *testing.T) {
	router, store, reader := routerWithInteractionClosedMetric(t)
	id := mustCreateGroup(t, store, "planning", "alice", "bob", "carol")
	router.SetEndVoteParams(id, 2, 3)

	require.NoError(t, endVote(t, router, id, "alice", "int-1"))   // turn 1
	require.NoError(t, plainTurn(t, router, id, "carol", "int-1")) // turn 2
	require.NoError(t, plainTurn(t, router, id, "carol", "int-1")) // turn 3
	require.NoError(t, endVote(t, router, id, "bob", "int-1"))     // turn 4 — alice (turn 1) is now stale

	var rm metricdata.ResourceMetrics
	require.NoError(t, reader.Collect(context.Background(), &rm))
	assert.Equal(t, int64(0), interactionClosedCount(t, rm, "group", "end_votes"),
		"alice's vote is outside the W=3 window of bob's, so no quorum")
}

// TestEndVote_UntrackedVoteIgnored pins that a vote with no interaction_id is
// never accumulated — there is nothing to scope the quorum to, so the layer
// stays at its inert default (additive). K=1 would close if it were tracked.
func TestEndVote_UntrackedVoteIgnored(t *testing.T) {
	router, store, reader := routerWithInteractionClosedMetric(t)
	id := mustCreateGroup(t, store, "planning", "alice", "bob")
	router.SetEndVoteParams(id, 1, 3)

	require.NoError(t, endVote(t, router, id, "alice", ""))
	require.NoError(t, endVote(t, router, id, "bob", ""))

	var rm metricdata.ResourceMetrics
	require.NoError(t, reader.Collect(context.Background(), &rm))
	assert.Equal(t, int64(0), interactionClosedCount(t, rm, "group", "end_votes"),
		"untracked (no interaction_id) votes are never accumulated")
}

// TestEndVote_NoVotesNeverCloses pins the opt-in posture: a tracked interaction
// with only ordinary traffic (no end-votes) never closes, and every publish
// fans out normally — defaults are behaviourally identical to v0.3.7.
func TestEndVote_NoVotesNeverCloses(t *testing.T) {
	router, store, reader := routerWithInteractionClosedMetric(t)
	disp := router.dispatcher.(*recordingDispatcher)
	id := mustCreateGroup(t, store, "planning", "alice", "bob")
	router.SetEndVoteParams(id, 2, 3)

	for i := 0; i < 5; i++ {
		require.NoError(t, plainTurn(t, router, id, "alice", "int-1"))
	}

	var rm metricdata.ResourceMetrics
	require.NoError(t, reader.Collect(context.Background(), &rm))
	assert.Equal(t, int64(0), interactionClosedCount(t, rm, "group", "end_votes"),
		"no end-votes → the interaction never closes")
	assert.Len(t, disp.snapshot(), 5, "every ordinary publish fans out to bob")
}

// TestEndVote_SpamLogged pins that a participant re-voting (vote tampering /
// spam) is logged so an adversarial pattern is visible in audit (§H). A high
// K keeps the interaction open so we observe the spam log, not a close.
func TestEndVote_SpamLogged(t *testing.T) {
	core, logs := observer.New(zap.WarnLevel)
	store := newTestStore(t, SQLiteOptions{})
	router := NewChannelRouter(store, &recordingDispatcher{}, zap.New(core), nil)
	id := mustCreateGroup(t, store, "planning", "alice", "bob")
	router.SetEndVoteParams(id, 5, 10)

	require.NoError(t, endVote(t, router, id, "alice", "int-1"))
	require.NoError(t, endVote(t, router, id, "alice", "int-1")) // re-vote → spam

	spam := logs.FilterMessageSnippet("vote").All()
	require.NotEmpty(t, spam, "a duplicate end-vote must be logged")
	assert.Equal(t, "int-1", spam[0].ContextMap()["interaction_id"])
}

// TestEndVote_StaleRevoteNotSpam pins that re-voting AFTER the prior vote has
// fallen out of the recency window is legitimate re-engagement, not vote
// tampering (§H) — so it must NOT be logged as spam. Only a still-live (in-
// window) duplicate is spam; otherwise a participant who votes early, waits out
// the window, and votes again pollutes the audit signal the layer exists to give.
func TestEndVote_StaleRevoteNotSpam(t *testing.T) {
	core, logs := observer.New(zap.WarnLevel)
	store := newTestStore(t, SQLiteOptions{})
	router := NewChannelRouter(store, &recordingDispatcher{}, zap.New(core), nil)
	id := mustCreateGroup(t, store, "planning", "alice", "bob")
	router.SetEndVoteParams(id, 5, 3) // high K so nothing closes; W=3

	require.NoError(t, endVote(t, router, id, "alice", "int-1")) // turn 1
	require.NoError(t, plainTurn(t, router, id, "bob", "int-1")) // turn 2
	require.NoError(t, plainTurn(t, router, id, "bob", "int-1")) // turn 3
	require.NoError(t, endVote(t, router, id, "alice", "int-1")) // turn 4 — prior (turn 1) is stale

	spam := logs.FilterMessageSnippet("vote").All()
	assert.Empty(t, spam, "a re-vote after the prior one went stale is not vote-spam")
}

// TestEndVote_CloseDiscardsReplyBudget pins that the close path drives the
// §F reset seam (DiscardInteractionReplyBudget): once the interaction closes
// on votes, a previously-exhausted participant regains its reply allowance.
func TestEndVote_CloseDiscardsReplyBudget(t *testing.T) {
	router, _, store := newRouterTest(t)
	id := mustCreateGroup(t, store, "planning", "alice", "bob", "carol")
	router.SetReplyBudget(id, 1)
	router.SetEndVoteParams(id, 2, 3)

	// carol exhausts her single reply slot in int-1.
	require.NoError(t, plainTurn(t, router, id, "carol", "int-1"))
	assert.ErrorIs(t, plainTurn(t, router, id, "carol", "int-1"), ErrParticipantBudgetExhausted)

	// alice + bob vote → close → reply counters for int-1 discarded.
	require.NoError(t, endVote(t, router, id, "alice", "int-1"))
	require.NoError(t, endVote(t, router, id, "bob", "int-1"))

	// carol's allowance is reset by the close.
	require.NoError(t, plainTurn(t, router, id, "carol", "int-1"),
		"the end-vote close must discard the per-interaction reply counters")
}

// TestEndVote_ExemptFromReplyBudget pins that an end-vote is never rejected by
// the Layer 2 reply budget: a vote is a terminal meta-signal, not a content
// reply, so a participant who has spent their reply allowance can still vote to
// terminate. Otherwise Layer 2 could starve Layer 4 — a budget-saturated
// brainstorm could never reach the quorum and never converge.
func TestEndVote_ExemptFromReplyBudget(t *testing.T) {
	router, store, reader := routerWithInteractionClosedMetric(t)
	id := mustCreateGroup(t, store, "planning", "alice", "bob", "carol")
	router.SetReplyBudget(id, 1)
	router.SetEndVoteParams(id, 2, 3)

	// alice and bob each spend their single reply slot on ordinary traffic.
	require.NoError(t, plainTurn(t, router, id, "alice", "int-1"))
	require.NoError(t, plainTurn(t, router, id, "bob", "int-1"))

	// Both are now at their reply cap, yet each must still be able to vote.
	require.NoError(t, endVote(t, router, id, "alice", "int-1"),
		"a budget-exhausted participant must still be able to vote to end")
	require.NoError(t, endVote(t, router, id, "bob", "int-1"),
		"a budget-exhausted participant must still be able to vote to end")

	var rm metricdata.ResourceMetrics
	require.NoError(t, reader.Collect(context.Background(), &rm))
	assert.Equal(t, int64(1), interactionClosedCount(t, rm, "group", "end_votes"),
		"votes are exempt from the reply budget, so the quorum still closes the interaction")
}

// TestEndVote_DiscardClearsAccumulator pins DiscardInteractionEndVotes: after a
// discard the prior vote is gone, so a single fresh vote cannot close a K=2
// interaction that the prior vote would otherwise have helped close.
func TestEndVote_DiscardClearsAccumulator(t *testing.T) {
	router, store, reader := routerWithInteractionClosedMetric(t)
	id := mustCreateGroup(t, store, "planning", "alice", "bob")
	router.SetEndVoteParams(id, 2, 3)

	require.NoError(t, endVote(t, router, id, "alice", "int-1"))
	router.DiscardInteractionEndVotes("int-1")
	require.NoError(t, endVote(t, router, id, "bob", "int-1"))

	var rm metricdata.ResourceMetrics
	require.NoError(t, reader.Collect(context.Background(), &rm))
	assert.Equal(t, int64(0), interactionClosedCount(t, rm, "group", "end_votes"),
		"a discarded interaction's votes do not carry into a fresh accumulator")
}

// TestEndVote_DoesNotResetCascadeDepth pins §H orthogonality: an end-vote does
// not touch the cascade_depth carried on the publish — the two mechanisms are
// independent (cap-reached vs. we're-done).
func TestEndVote_DoesNotResetCascadeDepth(t *testing.T) {
	router, _, store := newRouterTest(t)
	id := mustCreateGroup(t, store, "planning", "alice", "bob")
	router.SetEndVoteParams(id, 2, 3)

	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: id, SenderID: "alice", Content: "done",
		Metadata: map[string]any{
			interactionIDMetadataKey: "int-1",
			endVoteMetadataKey:       true,
			cascadeDepthMetadataKey:  2,
		},
	}, ""))

	hist, err := store.GetHistory(context.Background(), id, 10, time.Time{})
	require.NoError(t, err)
	require.Len(t, hist, 1)
	assert.EqualValues(t, 2, hist[0].Metadata[cascadeDepthMetadataKey],
		"the end-vote leaves cascade_depth untouched (orthogonal)")
}

// TestEndVote_ParamsDefaultWhenUnset pins that a channel with no resolved
// end-vote config reads the K=2 / W=3 defaults from EndVoteParamsFor.
func TestEndVote_ParamsDefaultWhenUnset(t *testing.T) {
	router, _, store := newRouterTest(t)
	id := mustCreateGroup(t, store, "planning", "alice")
	k, w := router.EndVoteParamsFor(id)
	assert.Equal(t, DefaultEndVoteThreshold, k)
	assert.Equal(t, DefaultEndVoteWindow, w)
}

// TestResolveEndVotes_AppliesPerChannelConfig pins that the startup resolver
// stamps a channel's declared (normalized) K/W onto the router so the publish
// path enforces the configured quorum.
func TestResolveEndVotes_AppliesPerChannelConfig(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	router := NewChannelRouter(store, &recordingDispatcher{}, zap.NewNop(), nil)
	cfg := &Config{
		Channels: []ChannelConfig{{
			Name:             "planning",
			EndVoteThreshold: 3,
			EndVoteWindow:    5,
			Members:          []MemberConfig{{ID: "alice", RespondPolicy: RespondAlways}},
		}},
	}
	require.NoError(t, router.ResolveEndVotes(context.Background(), cfg))
	k, w := router.EndVoteParamsFor("group:planning")
	assert.Equal(t, 3, k)
	assert.Equal(t, 5, w)
}
