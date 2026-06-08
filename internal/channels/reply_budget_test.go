package channels

import (
	"context"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
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

	require.NoError(t, publishReply(t, router, id, "alice", "int-1", "agent"))
	// alice's 2nd in int-1 is denied.
	assert.ErrorIs(t, publishReply(t, router, id, "alice", "int-1", "agent"), ErrParticipantBudgetExhausted)
	// bob in the same interaction is unaffected.
	require.NoError(t, publishReply(t, router, id, "bob", "int-1", "agent"))
	// alice in a *different* interaction has a fresh allowance.
	require.NoError(t, publishReply(t, router, id, "alice", "int-2", "agent"))
}

// TestReplyBudget_UntrackedInteractionUncapped pins that a publish with no
// interaction_id is never budget-gated — there is no Interaction to scope the
// counter to, so the layer stays at its uncapped default (additive).
func TestReplyBudget_UntrackedInteractionUncapped(t *testing.T) {
	router, _, store := newRouterTest(t)
	id := mustCreateGroup(t, store, "planning", "alice", "bob")
	router.SetReplyBudget(id, 1)

	for i := 0; i < 5; i++ {
		require.NoError(t, publishReply(t, router, id, "alice", "", "agent"))
	}
	hist, _ := store.GetHistory(context.Background(), id, 100, time.Time{})
	assert.Len(t, hist, 5, "untracked (no interaction_id) traffic is never budget-gated")
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

	require.NoError(t, publishReply(t, router, id, "alice", "int-1", "agent"))
	assert.ErrorIs(t, publishReply(t, router, id, "alice", "int-1", "agent"), ErrParticipantBudgetExhausted)

	// Close → discard the per-interaction counters.
	router.DiscardInteractionReplyBudget("int-1")

	// alice gets a fresh allowance after the reset.
	require.NoError(t, publishReply(t, router, id, "alice", "int-1", "agent"))
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
