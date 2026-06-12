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
// per-interaction governance counters are discarded (§H). Both producers are
// live: the resolver stamps every publish and overrides caller claims
// (interaction_resolver.go, producer plan PR 1, #604), and the Python
// END_INTERACTION_VOTE action publishes the flag (agents/end_vote_action.py,
// PR 2, #605). These tests still drive the flag directly through the publish
// metadata bag — the same wire shape the producer emits — to pin the
// orchestrator hook in isolation; the composed end-to-end arc is
// interaction_convergence_test.go.

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
// publish's fanout is suppressed — its only dispatches are the marked CP1
// close notifications (the end-vote-close-propagation amendment), never
// ordinary fanout that could draw new replies.
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

	// The closing vote's only dispatches are the close notifications to the
	// non-sender members (alice, carol) — marked, so a receiver treats them
	// as control; an UNMARKED post-close dispatch here would be leaked
	// fanout, exactly what suppression exists to stop.
	router.WaitForPendingFanout()
	postClose := disp.snapshot()[before:]
	notified := map[string]bool{}
	for _, call := range postClose {
		assert.True(t, call.closeNotification,
			"every post-close dispatch is a marked close notification, never fanout")
		notified[call.participantID] = true
	}
	assert.Equal(t, map[string]bool{"alice": true, "carol": true}, notified,
		"the close is announced to exactly the non-sender members")
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

// TestEndVote_PendingVoteSurvivesSilenceAndEscalationTurn pins the window
// mechanics the chair-stall-escalation amendment (OQ 1) leans on: W counts
// turns — publishes carrying the interaction_id — not wall-clock, so a
// zero-replied round advances zero turns and a lone pending vote cannot go
// stale during the very silence that triggers the escalation (only idle
// rotation kills it, by closing the whole interaction). The arc is the
// quorum-pending stall: a lone vote (turn 1), the stall itself (no publishes —
// deliberately nothing to drive here, which is the point), one escalation
// synthesis turn (turn 2), then a concurrence at turn 3 that still sees the
// first vote live (3-1 = 2 < W=3). Distance W-1 with an intervening publish is
// the window boundary no other test exercises (KDistinct… is distance 1,
// OutOfWindow… is distance W).
func TestEndVote_PendingVoteSurvivesSilenceAndEscalationTurn(t *testing.T) {
	router, store, reader := routerWithInteractionClosedMetric(t)
	id := mustCreateGroup(t, store, "planning", "alice", "bob", "carol")
	router.SetEndVoteParams(id, 2, 3)

	require.NoError(t, endVote(t, router, id, "alice", "int-1")) // turn 1 — the lone pending vote
	// The zero-replied round: no publishes, so the per-interaction turn
	// counter does not move and alice's vote stays live.
	require.NoError(t, plainTurn(t, router, id, "carol", "int-1")) // turn 2 — the chair's synthesis
	require.NoError(t, endVote(t, router, id, "bob", "int-1"))     // turn 3 — concurrence; alice is at distance 2 < W

	var rm metricdata.ResourceMetrics
	require.NoError(t, reader.Collect(context.Background(), &rm))
	assert.Equal(t, int64(1), interactionClosedCount(t, rm, "group", "end_votes"),
		"a pre-stall vote at distance W-1 is still live — the escalation arc completes the quorum")
}

// TestEndVote_UntrackedVoteIgnored pins that a vote with no interaction_id is
// never accumulated — there is nothing to scope the quorum to, so the layer
// stays at its inert default (additive). K=1 would close if it were tracked.
func TestEndVote_UntrackedVoteIgnored(t *testing.T) {
	// Since the interaction-id producer landed, every Publish is stamped with a
	// resolver-minted id — untracked traffic can no longer arise on the publish
	// path. The untracked tolerance is now a defence-in-depth property of the
	// hook itself (a caller bypassing the resolver), so pin it by direct call.
	router, store, _ := routerWithInteractionClosedMetric(t)
	id := mustCreateGroup(t, store, "planning", "alice", "bob")
	router.SetEndVoteParams(id, 1, 3)

	suppressed := router.processEndVote(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: id, SenderID: "alice", Content: "done",
		Metadata: map[string]any{endVoteMetadataKey: true}, // no interaction_id
	}, ChannelTypeGroup)

	assert.False(t, suppressed, "an untracked vote is a no-op, not a suppression")
	router.endVoteMu.Lock()
	accumulators := len(router.endVotes)
	router.endVoteMu.Unlock()
	assert.Zero(t, accumulators, "untracked (no interaction_id) votes are never accumulated")
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
	resolved := openInteractionID(router, id) // the resolver overrode the "int-1" claim (IP2)
	require.NotEmpty(t, resolved)
	require.NoError(t, endVote(t, router, id, "alice", "int-1")) // re-vote → spam

	spam := logs.FilterMessageSnippet("vote").All()
	require.NotEmpty(t, spam, "a duplicate end-vote must be logged")
	assert.Equal(t, resolved, spam[0].ContextMap()["interaction_id"],
		"the spam log carries the resolver-minted id, never the publisher's claim")
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

// TestEndVote_DuplicateLiveVoteSuppressesFanout pins the hardening that closes
// the reply-budget bypass opened by the Layer 2 exemption. An end-vote is exempt
// from the reply budget (so a budget-exhausted participant can still cast the
// terminating vote — see TestEndVote_ExemptFromReplyBudget), but a participant
// must not be able to weaponise that exemption by flagging every publish as a
// vote to flood the channel past their cap. A participant's FIRST vote is a real
// signal and fans out (others must see it); a duplicate IN-WINDOW vote is
// redundant for the quorum (deduped) and is suppressed from fanout, so repeated
// votes draw no new N-way amplification.
func TestEndVote_DuplicateLiveVoteSuppressesFanout(t *testing.T) {
	router, store, _ := routerWithInteractionClosedMetric(t)
	disp := router.dispatcher.(*recordingDispatcher)
	id := mustCreateGroup(t, store, "planning", "alice", "bob", "carol")
	router.SetEndVoteParams(id, 5, 10) // high K so nothing closes; wide W so the re-vote stays live

	// The first end-vote is a real signal — it fans out to the other members.
	require.NoError(t, endVote(t, router, id, "alice", "int-1"))
	afterFirst := len(disp.snapshot())
	require.Positive(t, afterFirst, "the first end-vote fans out to the other members")

	// A duplicate live vote is deduped to a no-op for the quorum; it must not
	// re-fan-out, or the vote-exemption from the reply budget becomes a bypass.
	require.NoError(t, endVote(t, router, id, "alice", "int-1"))
	assert.Equal(t, afterFirst, len(disp.snapshot()),
		"a duplicate in-window end-vote is suppressed from fanout (no Layer 2 bypass)")
}

// TestEndVote_StaleRevoteStillFansOut pins the other side of the suppression: a
// re-vote cast AFTER the prior one fell out of the recency window is legitimate
// re-engagement (not spam — see TestEndVote_StaleRevoteNotSpam), so it is a fresh
// signal and DOES fan out. Only a redundant in-window duplicate is suppressed.
func TestEndVote_StaleRevoteStillFansOut(t *testing.T) {
	router, store, _ := routerWithInteractionClosedMetric(t)
	disp := router.dispatcher.(*recordingDispatcher)
	id := mustCreateGroup(t, store, "planning", "alice", "bob")
	router.SetEndVoteParams(id, 5, 3) // high K so nothing closes; W=3

	require.NoError(t, endVote(t, router, id, "alice", "int-1")) // turn 1, fans out
	require.NoError(t, plainTurn(t, router, id, "bob", "int-1")) // turn 2
	require.NoError(t, plainTurn(t, router, id, "bob", "int-1")) // turn 3
	beforeRevote := len(disp.snapshot())
	require.NoError(t, endVote(t, router, id, "alice", "int-1")) // turn 4 — prior (turn 1) is stale

	assert.Greater(t, len(disp.snapshot()), beforeRevote,
		"a stale (out-of-window) re-vote is legitimate re-engagement and still fans out")
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

	require.NoError(t, endVote(t, router, id, "alice", ""))
	router.DiscardInteractionEndVotes(openInteractionID(router, id))
	require.NoError(t, endVote(t, router, id, "bob", ""))

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
