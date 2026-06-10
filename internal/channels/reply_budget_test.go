package channels

import (
	"context"
	"strings"
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

// publishReply is a small helper that drives a single Layer 2 publish for a
// given (sender, interaction, participant_type) tuple. Each publish gets a
// fresh UUID so the store's per-message identity is unique.
func publishReply(t *testing.T, router *ChannelRouter, channelID, sender, interactionID, participantType string) error {
	t.Helper()
	meta := map[string]any{}
	if interactionID != "" {
		meta[interactionIDMetadataKey] = interactionID
	}
	if participantType != "" {
		meta[participantTypeMetadataKey] = participantType
	}
	return router.Publish(context.Background(), ChannelMessage{
		ID:        uuid.NewString(),
		ChannelID: channelID,
		SenderID:  sender,
		Content:   "msg",
		Metadata:  meta,
	}, "")
}

// TestReplyBudget_KPlusOneRejectedPrePersistence pins the core Layer 2
// contract (RFC 0030 §F): with `max_replies_per_participant_per_interaction=K`
// a participant's (K+1)th publish in one interaction is rejected with
// ErrParticipantBudgetExhausted *before* persistence — the dropped message
// never enters channel history.
func TestReplyBudget_KPlusOneRejectedPrePersistence(t *testing.T) {
	router, _, store := newRouterTest(t)
	id := mustCreateGroup(t, store, "planning", "alice", "bob")
	router.SetReplyBudget(id, 2)

	// First two publishes in interaction "int-1" succeed.
	require.NoError(t, publishReply(t, router, id, "alice", "int-1", "agent"))
	require.NoError(t, publishReply(t, router, id, "alice", "int-1", "agent"))

	// The third (K+1) is rejected.
	err := publishReply(t, router, id, "alice", "int-1", "agent")
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrParticipantBudgetExhausted)

	// Pre-persistence: only the two accepted messages are in history.
	hist, herr := store.GetHistory(context.Background(), id, 100, time.Time{})
	require.NoError(t, herr)
	assert.Len(t, hist, 2, "the rejected (K+1)th publish must not be persisted")
}

// TestReplyBudget_StoreRejectedPublishDoesNotConsumeBudget pins that the §F
// counter only ever reflects messages that actually entered channel history: a
// publish the store rejects post-gate (here oversized content → 413) must NOT
// erode the sender's allowance. Regression — enforceReplyBudget once
// incremented the counter pre-persistence, so a member who tripped a store
// rejection K times was locked out (429) with zero messages in history.
func TestReplyBudget_StoreRejectedPublishDoesNotConsumeBudget(t *testing.T) {
	router, _, store := newRouterTest(t)
	id := mustCreateGroup(t, store, "planning", "alice", "bob")
	router.SetReplyBudget(id, 1)

	// alice's first publish is rejected by the store (oversized content) AFTER
	// the budget gate runs, so it never enters history — and must not consume
	// her single reply slot.
	oversized := strings.Repeat("x", MaxMessageContentBytes+1)
	err := router.Publish(context.Background(), ChannelMessage{
		ID:        uuid.NewString(),
		ChannelID: id,
		SenderID:  "alice",
		Content:   oversized,
		Metadata: map[string]any{
			interactionIDMetadataKey:   "int-1",
			participantTypeMetadataKey: "agent",
		},
	}, "")
	require.ErrorIs(t, err, ErrMessageContentTooLarge)

	// Budget intact: alice's first *valid* publish still lands.
	require.NoError(t, publishReply(t, router, id, "alice", "int-1", "agent"))
	// And only now is she at her cap — the next is correctly denied.
	assert.ErrorIs(t, publishReply(t, router, id, "alice", "int-1", "agent"), ErrParticipantBudgetExhausted)

	hist, herr := store.GetHistory(context.Background(), id, 100, time.Time{})
	require.NoError(t, herr)
	assert.Len(t, hist, 1, "only the one valid publish is in history")
}

// TestReplyBudget_PerParticipantAndPerInteraction proves the counter is keyed
// by (participant, interaction): a second participant is unaffected by alice's
// exhaustion, and the same participant gets a fresh allowance in a different
// interaction.
func TestReplyBudget_PerParticipantAndPerInteraction(t *testing.T) {
	router, _, store := newRouterTest(t)
	id := mustCreateGroup(t, store, "planning", "alice", "bob")
	router.SetReplyBudget(id, 1)
	now := time.Date(2026, 6, 10, 12, 0, 0, 0, time.UTC)
	router.interactionNow = func() time.Time { return now }

	require.NoError(t, publishReply(t, router, id, "alice", "", "agent"))
	// alice's 2nd in the open interaction is denied.
	assert.ErrorIs(t, publishReply(t, router, id, "alice", "", "agent"), ErrParticipantBudgetExhausted)
	// bob in the same interaction is unaffected.
	require.NoError(t, publishReply(t, router, id, "bob", "", "agent"))
	// alice in a *different* interaction — the idle window rotates the channel
	// onto a fresh one — has a fresh allowance.
	now = now.Add(601 * time.Second)
	require.NoError(t, publishReply(t, router, id, "alice", "", "agent"))
}

// TestReplyBudget_UntrackedInteractionUncapped pins that an untracked message
// (no interaction_id) is never budget-gated — there is no Interaction to scope
// the counter to, so the layer stays at its uncapped default (additive). Since
// the interaction-id producer landed, every Publish is stamped — untracked
// input can only reach the hook if the resolver is bypassed, so the tolerance
// is pinned by direct call (the TestEndVote_UntrackedVoteIgnored posture).
func TestReplyBudget_UntrackedInteractionUncapped(t *testing.T) {
	router, _, store := newRouterTest(t)
	id := mustCreateGroup(t, store, "planning", "alice", "bob")
	router.SetReplyBudget(id, 1)

	for i := 0; i < 5; i++ {
		release, err := router.enforceReplyBudget(context.Background(), ChannelMessage{
			ID: uuid.NewString(), ChannelID: id, SenderID: "alice", Content: "hi",
		}, ChannelTypeGroup)
		require.NoError(t, err, "untracked (no interaction_id) traffic is never budget-gated")
		require.Nil(t, release, "no reservation is taken for untracked traffic")
	}
}

// TestReplyBudget_HumanPrincipalExempt pins GL4 / §OQ-7: a human principal
// (participant_type "user") is exempt from the reply budget when "human" is in
// governance.exempt_principals.
func TestReplyBudget_HumanPrincipalExempt(t *testing.T) {
	router, _, store := newRouterTest(t)
	id := mustCreateGroup(t, store, "planning", "alice", "User_1")
	router.SetReplyBudget(id, 1)
	router.SetExemptPrincipals([]string{"human"})

	// An agent is capped at 1.
	require.NoError(t, publishReply(t, router, id, "alice", "int-1", "agent"))
	assert.ErrorIs(t, publishReply(t, router, id, "alice", "int-1", "agent"), ErrParticipantBudgetExhausted)

	// A human principal publishes well past the cap unimpeded.
	for i := 0; i < 4; i++ {
		require.NoError(t, publishReply(t, router, id, "User_1", "int-1", "user"))
	}
}

// TestReplyBudget_DefaultZeroPreservesBehaviour pins the opt-in default: with
// no budget set (K=0/uncapped), publishing behaves exactly as v0.3.0 — every
// publish lands.
func TestReplyBudget_DefaultZeroPreservesBehaviour(t *testing.T) {
	router, _, store := newRouterTest(t)
	id := mustCreateGroup(t, store, "planning", "alice", "bob")
	// No SetReplyBudget call → uncapped.

	for i := 0; i < 6; i++ {
		require.NoError(t, publishReply(t, router, id, "alice", "int-1", "agent"))
	}
	hist, _ := store.GetHistory(context.Background(), id, 100, time.Time{})
	assert.Len(t, hist, 6, "default (uncapped) budget preserves v0.3.0 behaviour")
}

// TestReplyBudget_CountersResetOnClose pins the §F reset semantics: discarding
// an interaction's counters (the seam the Layer 4 / RFC 0020 close path drives)
// restores a fresh allowance.
func TestReplyBudget_CountersResetOnClose(t *testing.T) {
	router, _, store := newRouterTest(t)
	id := mustCreateGroup(t, store, "planning", "alice", "bob")
	router.SetReplyBudget(id, 1)

	require.NoError(t, publishReply(t, router, id, "alice", "", "agent"))
	assert.ErrorIs(t, publishReply(t, router, id, "alice", "", "agent"), ErrParticipantBudgetExhausted)

	// Close → discard the per-interaction counters (the resolver-minted id is
	// the live key since the producer landed).
	router.DiscardInteractionReplyBudget(openInteractionID(router, id))

	// alice gets a fresh allowance after the reset.
	require.NoError(t, publishReply(t, router, id, "alice", "", "agent"))
	_ = store
}

// TestResolveReplyBudgets_WarnsOnAllParticipantUncapped pins the advisory
// startup Warn: a channel whose membership is all-`participant` (all-`always`)
// with an uncapped reply budget gets a startup Warn — operators are told the
// pile-on guard is off. It is advisory only (not a behaviour change).
func TestResolveReplyBudgets_WarnsOnAllParticipantUncapped(t *testing.T) {
	core, logs := observer.New(zap.WarnLevel)
	store := newTestStore(t, SQLiteOptions{})
	router := NewChannelRouter(store, &recordingDispatcher{}, zap.New(core), nil)

	cfg := &Config{
		Channels: []ChannelConfig{{
			Name: "planning",
			Members: []MemberConfig{
				{ID: "alice", RespondPolicy: RespondAlways},
				{ID: "bob", RespondPolicy: RespondAlways},
			},
			// No max_replies_per_participant_per_interaction → uncapped.
		}},
	}
	require.NoError(t, router.ResolveReplyBudgets(context.Background(), cfg))

	warns := logs.FilterMessageSnippet("reply budget").All()
	require.NotEmpty(t, warns, "expected an all-participant + uncapped reply-budget warning")
	assert.Equal(t, "group:planning", warns[0].ContextMap()["channel_id"])
}

// TestResolveReplyBudgets_NoWarnWhenCapped proves the Warn does NOT fire when
// the channel declares a reply budget — the guard is on, so no advisory.
func TestResolveReplyBudgets_NoWarnWhenCapped(t *testing.T) {
	core, logs := observer.New(zap.WarnLevel)
	store := newTestStore(t, SQLiteOptions{})
	router := NewChannelRouter(store, &recordingDispatcher{}, zap.New(core), nil)

	cfg := &Config{
		Channels: []ChannelConfig{{
			Name: "planning",
			Members: []MemberConfig{
				{ID: "alice", RespondPolicy: RespondAlways},
				{ID: "bob", RespondPolicy: RespondAlways},
			},
			MaxRepliesPerParticipantPerInteraction: 3,
		}},
	}
	require.NoError(t, router.ResolveReplyBudgets(context.Background(), cfg))
	assert.Empty(t, logs.FilterMessageSnippet("reply budget").All(), "a capped channel must not warn")
	assert.Equal(t, 3, router.ReplyBudgetFor("group:planning"))
}

// TestReplyBudget_GovernanceDropCounter pins the telemetry contract for the
// drop path: a Layer 2 rejection fires the shared
// `channel.conversation.governance_drop` counter exactly once, labelled
// `channel_type` + `layer=reply_budget` — the instrument PR 5 reuses for the
// `cost`/`depth`/`end_vote` layers, so the label is the join point the
// governance-drop dashboard keys on. The accepted publish that precedes the
// drop must NOT increment it (value is 1, not 2), so the counter tracks drops,
// not publishes. Mirrors the floor-control telemetry test's manual-reader shape.
func TestReplyBudget_GovernanceDropCounter(t *testing.T) {
	reader := sdkmetric.NewManualReader()
	mp := sdkmetric.NewMeterProvider(sdkmetric.WithReader(reader))
	t.Cleanup(func() { _ = mp.Shutdown(context.Background()) })
	dropCtr, err := mp.Meter("test").Int64Counter("channel.conversation.governance_drop")
	require.NoError(t, err)

	store := newTestStore(t, SQLiteOptions{})
	router := NewChannelRouter(store, &recordingDispatcher{}, zap.NewNop(), &RouterMetrics{
		GovernanceDrop: dropCtr,
	})
	id := mustCreateGroup(t, store, "planning", "alice", "bob")
	router.SetReplyBudget(id, 1)

	// One accepted publish (no drop), then the (K+1)th is rejected (one drop).
	require.NoError(t, publishReply(t, router, id, "alice", "int-1", "agent"))
	assert.ErrorIs(t, publishReply(t, router, id, "alice", "int-1", "agent"), ErrParticipantBudgetExhausted)

	var rm metricdata.ResourceMetrics
	require.NoError(t, reader.Collect(context.Background(), &rm))
	assert.Equal(t, int64(1), governanceDropCount(t, rm, "group", "reply_budget"),
		"exactly one governance_drop{channel_type=group,layer=reply_budget} for the single (K+1)th drop")
}

// governanceDropCount returns the governance_drop counter value for the given
// channel_type + layer attribute pair, or 0 if no matching data point exists.
func governanceDropCount(t *testing.T, rm metricdata.ResourceMetrics, channelType, layer string) int64 {
	t.Helper()
	for _, sm := range rm.ScopeMetrics {
		for _, m := range sm.Metrics {
			if m.Name != "channel.conversation.governance_drop" {
				continue
			}
			sum, ok := m.Data.(metricdata.Sum[int64])
			require.Truef(t, ok, "governance_drop: expected Sum[int64], got %T", m.Data)
			for _, dp := range sum.DataPoints {
				ct, _ := dp.Attributes.Value("channel_type")
				ly, _ := dp.Attributes.Value("layer")
				if ct.AsString() == channelType && ly.AsString() == layer {
					return dp.Value
				}
			}
		}
	}
	return 0
}
