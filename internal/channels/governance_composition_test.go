package channels

import (
	"context"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.opentelemetry.io/otel"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
	"go.uber.org/zap"
)

// RFC 0030 deterministic governance layers (v0.3.8) — composition + telemetry
// closeout (PR 5). These tests pin the §B composition contract across the layers
// the channel publish path owns (Layer 0 depth, Layer 2 reply budget, Layer 4
// end-vote): a lower-layer drop short-circuits the higher layers and is
// attributed by `governance_drop{layer}`; with every layer at its default the
// path is behaviourally identical to v0.3.7 (GL1). The layers are inert in
// production (no `interaction_id` producer); these drive them through the publish
// metadata bag, like the per-layer suites.

// routerWithGovernanceMetrics builds a router whose full v0.3.8 governance-layer
// telemetry surface is collectible through one manual reader, so a composition
// test can assert every counter/histogram from a single Collect.
func routerWithGovernanceMetrics(t *testing.T) (*ChannelRouter, ChannelStore, *sdkmetric.ManualReader) {
	t.Helper()
	reader := sdkmetric.NewManualReader()
	mp := sdkmetric.NewMeterProvider(sdkmetric.WithReader(reader))
	t.Cleanup(func() { _ = mp.Shutdown(context.Background()) })
	m := mp.Meter("test")
	drop, err := m.Int64Counter("channel.conversation.governance_drop")
	require.NoError(t, err)
	closed, err := m.Int64Counter("channel.conversation.interaction_closed")
	require.NoError(t, err)
	emitted, err := m.Int64Counter("channel.conversation.end_vote_emitted")
	require.NoError(t, err)
	remaining, err := m.Float64Histogram("channel.conversation.reply_budget_remaining")
	require.NoError(t, err)
	capped, err := m.Int64Counter("channel.messages.cascade_capped")
	require.NoError(t, err)
	store := newTestStore(t, SQLiteOptions{})
	router := NewChannelRouter(store, &recordingDispatcher{}, zap.NewNop(), &RouterMetrics{
		GovernanceDrop:        drop,
		InteractionClosed:     closed,
		EndVoteEmitted:        emitted,
		ReplyBudgetRemaining:  remaining,
		MessagesCascadeCapped: capped,
	})
	return router, store, reader
}

// endVoteEmittedCount returns the end_vote_emitted counter value for a
// channel_type, or 0 when no matching point exists.
func endVoteEmittedCount(t *testing.T, rm metricdata.ResourceMetrics, channelType string) int64 {
	t.Helper()
	for _, sm := range rm.ScopeMetrics {
		for _, m := range sm.Metrics {
			if m.Name != "channel.conversation.end_vote_emitted" {
				continue
			}
			sum, ok := m.Data.(metricdata.Sum[int64])
			require.Truef(t, ok, "end_vote_emitted: expected Sum[int64], got %T", m.Data)
			for _, dp := range sum.DataPoints {
				if ct, _ := dp.Attributes.Value("channel_type"); ct.AsString() == channelType {
					return dp.Value
				}
			}
		}
	}
	return 0
}

// replyBudgetRemainingHist returns the (count, sum) of the reply_budget_remaining
// histogram for a channel_type, or (0, 0) when no matching point exists.
func replyBudgetRemainingHist(t *testing.T, rm metricdata.ResourceMetrics, channelType string) (uint64, float64) {
	t.Helper()
	for _, sm := range rm.ScopeMetrics {
		for _, m := range sm.Metrics {
			if m.Name != "channel.conversation.reply_budget_remaining" {
				continue
			}
			hist, ok := m.Data.(metricdata.Histogram[float64])
			require.Truef(t, ok, "reply_budget_remaining: expected Histogram[float64], got %T", m.Data)
			for _, dp := range hist.DataPoints {
				if ct, _ := dp.Attributes.Value("channel_type"); ct.AsString() == channelType {
					return dp.Count, dp.Sum
				}
			}
		}
	}
	return 0, 0
}

// cascadeCappedCount returns the channel.messages.cascade_capped counter value
// for a channel_type, or 0 when absent.
func cascadeCappedCount(t *testing.T, rm metricdata.ResourceMetrics, channelType string) int64 {
	t.Helper()
	for _, sm := range rm.ScopeMetrics {
		for _, m := range sm.Metrics {
			if m.Name != "channel.messages.cascade_capped" {
				continue
			}
			sum, ok := m.Data.(metricdata.Sum[int64])
			require.Truef(t, ok, "cascade_capped: expected Sum[int64], got %T", m.Data)
			for _, dp := range sum.DataPoints {
				if ct, _ := dp.Attributes.Value("channel_type"); ct.AsString() == channelType {
					return dp.Value
				}
			}
		}
	}
	return 0
}

func collect(t *testing.T, reader *sdkmetric.ManualReader) metricdata.ResourceMetrics {
	t.Helper()
	var rm metricdata.ResourceMetrics
	require.NoError(t, reader.Collect(context.Background(), &rm))
	return rm
}

// TestGovernance_BackCompat_DefaultsAreInert pins GL1: with every layer at its
// default (uncapped reply budget, depth under cap, no votes, untracked traffic)
// a multi-persona publish fans out to all non-sender members and persists,
// exactly as in v0.3.7 — and NO governance telemetry fires. The whole feature is
// opt-in: defaults change nothing.
func TestGovernance_BackCompat_DefaultsAreInert(t *testing.T) {
	router, store, reader := routerWithGovernanceMetrics(t)
	disp := router.dispatcher.(*recordingDispatcher)
	id := mustCreateGroup(t, store, "planning", "alice", "bob", "carol", "dave")

	// An ordinary open-floor publish: no interaction_id, no vote, depth 0.
	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: id, SenderID: "alice", Content: "open question",
	}, ""))

	// Fanout topology is the pre-governance baseline: every non-sender member.
	calls := disp.snapshot()
	assert.Len(t, calls, 3, "all non-sender members fanned out — defaults are inert")

	// The message is in history.
	msgs, err := store.GetHistory(context.Background(), id, 100, time.Time{})
	require.NoError(t, err)
	assert.Len(t, msgs, 1, "the publish persisted")

	// No governance layer fired.
	rm := collect(t, reader)
	for _, layer := range []string{governanceLayerDepth, governanceLayerReplyBudget, governanceLayerEndVote} {
		assert.Zero(t, governanceDropCount(t, rm, "group", layer), "no governance_drop{layer=%s} on default traffic", layer)
	}
	assert.Zero(t, interactionClosedCount(t, rm, "group", endVotesTrigger), "no interaction closed on default traffic")
	assert.Zero(t, endVoteEmittedCount(t, rm, "group"), "no votes on default traffic")
	cnt, _ := replyBudgetRemainingHist(t, rm, "group")
	assert.Zero(t, cnt, "no reply-budget-remaining points on default traffic")
}

// TestGovernance_ReplyBudgetDropShortCircuitsFanout pins the composition rule for
// Layer 2 (pre-persistence): an over-budget (K+1)th publish is rejected before
// the store commit, so it never persists, never fans out, and never reaches the
// higher post-persistence layers — attributed by governance_drop{layer=reply_budget}.
func TestGovernance_ReplyBudgetDropShortCircuitsFanout(t *testing.T) {
	router, store, reader := routerWithGovernanceMetrics(t)
	disp := router.dispatcher.(*recordingDispatcher)
	id := mustCreateGroup(t, store, "planning", "alice", "bob")
	router.SetReplyBudget(id, 1)

	// First publish in the interaction is admitted and fans out to bob.
	require.NoError(t, publishReply(t, router, id, "alice", "int-1", "agent"))
	require.Len(t, disp.snapshot(), 1, "first publish fanned out")

	// The (K+1)th is rejected pre-persistence and short-circuits everything below.
	err := publishReply(t, router, id, "alice", "int-1", "agent")
	require.ErrorIs(t, err, ErrParticipantBudgetExhausted)

	assert.Len(t, disp.snapshot(), 1, "rejected publish did not fan out (short-circuit)")
	msgs, mErr := store.GetHistory(context.Background(), id, 100, time.Time{})
	require.NoError(t, mErr)
	assert.Len(t, msgs, 1, "rejected publish never entered history")

	rm := collect(t, reader)
	assert.Equal(t, int64(1), governanceDropCount(t, rm, "group", governanceLayerReplyBudget),
		"the drop is attributed to layer=reply_budget")
}

// TestGovernance_DepthCapEmitsGovernanceDrop pins that the Layer 0 cascade cap —
// which persists the publish but suppresses fanout — now also attributes itself
// on the shared governance_drop{layer=depth} counter (in addition to the legacy
// cascade_capped counter), so every governance layer is visible on one dashboard.
func TestGovernance_DepthCapEmitsGovernanceDrop(t *testing.T) {
	router, store, reader := routerWithGovernanceMetrics(t)
	disp := router.dispatcher.(*recordingDispatcher)
	router.SetMaxCascadeDepth(2)
	id := mustCreateGroup(t, store, "planning", "alice", "bob", "carol")

	// A publish already at the cap: persists (2xx) but its cascade is terminated.
	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: id, SenderID: "alice", Content: "deep",
		Metadata: map[string]any{cascadeDepthMetadataKey: 2},
	}, ""))

	assert.Empty(t, disp.snapshot(), "at-cap publish suppresses fanout")
	msgs, err := store.GetHistory(context.Background(), id, 100, time.Time{})
	require.NoError(t, err)
	assert.Len(t, msgs, 1, "the capped publish still persisted (2xx)")

	rm := collect(t, reader)
	assert.Equal(t, int64(1), governanceDropCount(t, rm, "group", governanceLayerDepth),
		"the cascade cap attributes itself on governance_drop{layer=depth}")
	assert.Equal(t, int64(2), cascadeCappedCount(t, rm, "group"),
		"the legacy per-recipient cascade_capped counter still ticks by suppressed recipients")
}

// TestGovernance_EndVoteEmittedCountsEveryVote pins the end_vote_emitted volume
// counter: every vote action increments it once (first vote, then a deduped
// in-window re-vote), independent of whether the quorum closed.
func TestGovernance_EndVoteEmittedCountsEveryVote(t *testing.T) {
	router, store, reader := routerWithGovernanceMetrics(t)
	id := mustCreateGroup(t, store, "planning", "alice", "bob")
	router.SetEndVoteParams(id, 2, 3) // K=2, W=3 — one voter cannot close.

	require.NoError(t, endVote(t, router, id, "alice", "int-1")) // first vote
	require.NoError(t, endVote(t, router, id, "alice", "int-1")) // in-window re-vote (spam)

	rm := collect(t, reader)
	assert.Equal(t, int64(2), endVoteEmittedCount(t, rm, "group"), "every vote action is counted")
	assert.Equal(t, int64(1), governanceDropCount(t, rm, "group", governanceLayerEndVote),
		"the suppressed duplicate vote is attributed on governance_drop{layer=end_vote}")
	assert.Zero(t, interactionClosedCount(t, rm, "group", endVotesTrigger), "one voter cannot reach K=2")
}

// TestGovernance_ReplyBudgetRemainingRecordedAtClose pins the Layer 4 → Layer 2
// composition seam: when the end-vote quorum closes an interaction, each tracked
// participant's leftover reply allowance (K - replies_used) is recorded on the
// reply_budget_remaining histogram before the counters are discarded.
func TestGovernance_ReplyBudgetRemainingRecordedAtClose(t *testing.T) {
	router, store, reader := routerWithGovernanceMetrics(t)
	id := mustCreateGroup(t, store, "planning", "alice", "bob")
	router.SetReplyBudget(id, 3) // K=3
	router.SetEndVoteParams(id, 2, 3)

	// alice spends 2 of 3 (remaining 1); bob spends 1 of 3 (remaining 2).
	require.NoError(t, publishReply(t, router, id, "alice", "int-1", "agent"))
	require.NoError(t, publishReply(t, router, id, "alice", "int-1", "agent"))
	require.NoError(t, publishReply(t, router, id, "bob", "int-1", "agent"))

	// Two distinct votes within the window close the interaction.
	require.NoError(t, endVote(t, router, id, "alice", "int-1"))
	require.NoError(t, endVote(t, router, id, "bob", "int-1"))

	rm := collect(t, reader)
	require.Equal(t, int64(1), interactionClosedCount(t, rm, "group", endVotesTrigger), "the interaction closed on votes")

	cnt, sum := replyBudgetRemainingHist(t, rm, "group")
	assert.Equal(t, uint64(2), cnt, "one remaining-allowance point per tracked participant")
	assert.Equal(t, float64(3), sum, "alice left 1 + bob left 2 = 3 leftover replies")
}

// TestGovernance_PostCloseSuppressionEmitsGovernanceDrop pins Layer 4's dominant
// effect: once an interaction has closed, EVERY later publish to it is suppressed
// from fanout — and that suppression IS a governance drop. Without attributing
// it, a converged conversation's post-close traffic (the bulk of Layer 4 drops in
// steady state) is invisible to `governance_drop{layer=end_vote}`, contradicting
// the counter's contract ("each publish dropped by a deterministic layer") and
// breaking symmetry with Layer 2 (which counts every rejected publish). The close
// event itself is NOT a drop — it is the `interaction_closed` signal.
func TestGovernance_PostCloseSuppressionEmitsGovernanceDrop(t *testing.T) {
	exporter := installSpanRecorder(t)
	router, store, reader := routerWithGovernanceMetrics(t)
	disp := router.dispatcher.(*recordingDispatcher)
	id := mustCreateGroup(t, store, "planning", "alice", "bob", "carol")
	router.SetEndVoteParams(id, 2, 3) // K=2, W=3

	// Two distinct votes close the interaction. The first vote fans out; the
	// closing vote suppresses its own fanout but is the close, not a drop.
	require.NoError(t, endVote(t, router, id, "alice", "int-1"))
	require.NoError(t, endVote(t, router, id, "bob", "int-1"))
	require.Zero(t, governanceDropCount(t, collect(t, reader), "group", governanceLayerEndVote),
		"reaching quorum is the interaction_closed signal, not a governance_drop")
	fannedBeforePostClose := len(disp.snapshot())

	// An ordinary (non-vote) reply to the now-closed interaction, carrying a
	// publish span so the trace-correlation stamp is observable too.
	ctx, span := otel.Tracer("test").Start(context.Background(), "publish")
	require.NoError(t, router.Publish(ctx, ChannelMessage{
		ID: uuid.NewString(), ChannelID: id, SenderID: "carol", Content: "late reply",
		Metadata: map[string]any{interactionIDMetadataKey: "int-1"},
	}, ""))
	span.End()

	assert.Len(t, disp.snapshot(), fannedBeforePostClose,
		"the post-close publish was suppressed from fanout (Layer 4)")
	rm := collect(t, reader)
	assert.Equal(t, int64(1), governanceDropCount(t, rm, "group", governanceLayerEndVote),
		"the post-close suppression is attributed on governance_drop{layer=end_vote}")

	spans := filterSpansByName(exporter.GetSpans(), "publish")
	require.Len(t, spans, 1, "the post-close publish span was recorded")
	assert.Equal(t, governanceLayerEndVote, spanAttrMap(spans[0])[governanceSpanLayerAttr],
		"the post-close drop stamped conversation.governance.layer on the publish span")
}

// TestGovernance_DropAnnotatesTraceSpan pins the §L trace-correlation contract:
// a governance drop stamps `conversation.governance.layer=<layer>` on the
// inbound publish span so an operator can find dropped publishes by a trace
// query, not only a log grep.
func TestGovernance_DropAnnotatesTraceSpan(t *testing.T) {
	exporter := installSpanRecorder(t)
	router, store, _ := routerWithGovernanceMetrics(t)
	id := mustCreateGroup(t, store, "planning", "alice", "bob")
	router.SetReplyBudget(id, 1)

	ctx, span := otel.Tracer("test").Start(context.Background(), "publish")
	require.NoError(t, publishReply(t, router, id, "alice", "int-1", "agent")) // admitted
	// (K+1)th drop carries the span in ctx.
	_, err := publishReplyCtx(ctx, t, router, id, "alice", "int-1", "agent")
	require.ErrorIs(t, err, ErrParticipantBudgetExhausted)
	span.End()

	spans := filterSpansByName(exporter.GetSpans(), "publish")
	require.Len(t, spans, 1, "the publish span was recorded")
	assert.Equal(t, governanceLayerReplyBudget, spanAttrMap(spans[0])[governanceSpanLayerAttr],
		"the drop stamped conversation.governance.layer on the publish span")
}

// publishReplyCtx is publishReply with an explicit context so a test can pass a
// span-carrying ctx through the publish path.
func publishReplyCtx(ctx context.Context, t *testing.T, router *ChannelRouter, channelID, sender, interactionID, participantType string) (struct{}, error) {
	t.Helper()
	meta := map[string]any{}
	if interactionID != "" {
		meta[interactionIDMetadataKey] = interactionID
	}
	if participantType != "" {
		meta[participantTypeMetadataKey] = participantType
	}
	return struct{}{}, router.Publish(ctx, ChannelMessage{
		ID: uuid.NewString(), ChannelID: channelID, SenderID: sender, Content: "msg", Metadata: meta,
	}, "")
}
