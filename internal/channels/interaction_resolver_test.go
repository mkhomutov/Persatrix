package channels

// interaction_resolver_test.go — the RFC 0030 interaction-id producer
// (docs/rfcs/0030-interaction-id-producer-pr-plan.md, PR 1). TDD-first: this
// matrix was written red against the planned resolver API and pins the IP1–IP8
// decisions — resolve-or-mint on the publish path, authoritative override of
// inbound claims, lazy idle rotation with one-generation-deferred discard
// seams, the thread exemption, and the Layer 4 close → resolver hook.

import (
	"context"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
	"go.uber.org/zap"
)

// publishAndGetInteractionID publishes a plain message and returns the
// interaction_id the router stamped onto the *persisted* row — the same value
// the fanout lift carries to `ChannelMessageEvent.interaction_id`.
func publishAndGetInteractionID(t *testing.T, router *ChannelRouter, store ChannelStore, channelID, sender string, metadata map[string]any) string {
	t.Helper()
	id := uuid.NewString()
	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: id, ChannelID: channelID, SenderID: sender, Content: "hi", Metadata: metadata,
	}, ""))
	msg, err := store.GetMessage(context.Background(), id)
	require.NoError(t, err)
	got, _ := msg.Metadata[interactionIDMetadataKey].(string)
	return got
}

// openInteractionID returns the channel's current open interaction id ("" when
// none). White-box read of the resolver table — tests that need "the id this
// channel's publishes are being stamped with" without publishing.
func openInteractionID(r *ChannelRouter, channelID string) string {
	r.interactionMu.Lock()
	defer r.interactionMu.Unlock()
	if e := r.openInteractions[channelID]; e != nil {
		return e.id
	}
	return ""
}

// reseedOpenInteraction force-sets the channel's open interaction — the
// race simulator. With the resolver authoritative (IP2), traffic carrying a
// retired/closed id cannot arise through Publish; the one real path is a
// commit that resolved the old id just before a rotation or quorum close
// landed (IP4's deferral exists for exactly this interleaving). Reseeding
// reproduces that interleaving deterministically: the next Publish resolves
// the seeded id exactly as the racing commit did.
func reseedOpenInteraction(r *ChannelRouter, channelID, interactionID string) {
	r.interactionMu.Lock()
	defer r.interactionMu.Unlock()
	r.openInteractions[channelID] = &openInteraction{id: interactionID, lastActivity: r.interactionNow()}
}

// resolverHarness builds a router with a controllable resolver clock and a
// 3-member group channel. Advance the clock through the returned *time.Time.
func resolverHarness(t *testing.T) (*ChannelRouter, ChannelStore, string, *time.Time) {
	t.Helper()
	store := newTestStore(t, SQLiteOptions{})
	router := NewChannelRouter(store, &envelopeRecorder{}, zap.NewNop(), nil)
	now := time.Date(2026, 6, 10, 12, 0, 0, 0, time.UTC)
	router.interactionNow = func() time.Time { return now }
	id := mustCreateGroupWithPolicies(t, store, "planning",
		map[string]RespondPolicy{
			"ember-owl": RespondAlways,
			"iron-fox":  RespondAlways,
		}, "ember-owl", "iron-fox")
	return router, store, id, &now
}

// TestInteractionResolver_StampsAndReusesWithinWindow — the producer's core
// contract (IP1): every publish to a tracked channel carries a router-minted,
// non-empty interaction_id, and publishes within the idle window share it.
func TestInteractionResolver_StampsAndReusesWithinWindow(t *testing.T) {
	router, store, ch, now := resolverHarness(t)

	first := publishAndGetInteractionID(t, router, store, ch, "ember-owl", nil)
	require.NotEmpty(t, first, "a publish to a tracked channel is stamped with a minted interaction_id")

	*now = now.Add(599 * time.Second)
	second := publishAndGetInteractionID(t, router, store, ch, "iron-fox", nil)
	assert.Equal(t, first, second, "publishes within the idle window share the open interaction")
}

// TestInteractionResolver_RotatesAfterIdleWindow — IP4: a publish past the
// idle window retires the open id, mints a fresh one, and emits
// `interaction_closed{trigger=idle}`.
func TestInteractionResolver_RotatesAfterIdleWindow(t *testing.T) {
	router, store, reader := routerWithInteractionClosedMetric(t)
	now := time.Date(2026, 6, 10, 12, 0, 0, 0, time.UTC)
	router.interactionNow = func() time.Time { return now }
	ch := mustCreateGroupWithPolicies(t, store, "planning",
		map[string]RespondPolicy{"ember-owl": RespondAlways, "iron-fox": RespondAlways},
		"ember-owl", "iron-fox")

	first := publishAndGetInteractionID(t, router, store, ch, "ember-owl", nil)
	now = now.Add(601 * time.Second)
	second := publishAndGetInteractionID(t, router, store, ch, "iron-fox", nil)

	require.NotEmpty(t, second)
	assert.NotEqual(t, first, second, "a publish past the idle window mints a fresh interaction")
	var rm metricdata.ResourceMetrics
	require.NoError(t, reader.Collect(context.Background(), &rm))
	assert.Equal(t, int64(1), interactionClosedCount(t, rm, "group", "idle"),
		"the idle rotation emits interaction_closed{trigger=idle}")
}

// TestInteractionResolver_DeferredDiscardFiresNextRotation — IP4's
// one-generation grace: rotating away from an id does NOT discard its
// governance state; the discard seams fire at the channel's *next* rotation,
// after every in-flight commit has long drained.
func TestInteractionResolver_DeferredDiscardFiresNextRotation(t *testing.T) {
	router, store, ch, now := resolverHarness(t)
	router.SetReplyBudget(ch, 5) // materialize replyCounts for the open id

	first := publishAndGetInteractionID(t, router, store, ch, "ember-owl", nil)

	*now = now.Add(601 * time.Second)
	second := publishAndGetInteractionID(t, router, store, ch, "iron-fox", nil)
	require.NotEqual(t, first, second)

	router.replyBudgetMu.Lock()
	_, firstAlive := router.replyCounts[first]
	router.replyBudgetMu.Unlock()
	assert.True(t, firstAlive,
		"the retired id's reply-budget state survives its own rotation (deferred seam)")

	*now = now.Add(601 * time.Second)
	third := publishAndGetInteractionID(t, router, store, ch, "ember-owl", nil)
	require.NotEqual(t, second, third)

	router.replyBudgetMu.Lock()
	_, firstAlive = router.replyCounts[first]
	_, secondAlive := router.replyCounts[second]
	router.replyBudgetMu.Unlock()
	assert.False(t, firstAlive,
		"the next rotation fires the deferred discard for the first retiree")
	assert.True(t, secondAlive,
		"the second id is now the pending retiree, its state intact")
}

// TestInteractionResolver_ThreadNeverRotates — IP3: a thread channel is one
// interaction for its whole life; the idle window is ignored.
func TestInteractionResolver_ThreadNeverRotates(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	router := NewChannelRouter(store, &envelopeRecorder{}, zap.NewNop(), nil)
	now := time.Date(2026, 6, 10, 12, 0, 0, 0, time.UTC)
	router.interactionNow = func() time.Time { return now }
	threadID := "thread:" + uuid.NewString()
	require.NoError(t, store.CreateChannel(context.Background(), Channel{ID: threadID, Type: ChannelTypeThread}))
	require.NoError(t, store.AddMember(context.Background(), threadID, "ember-owl", RespondAlways))
	require.NoError(t, store.AddMember(context.Background(), threadID, "iron-fox", RespondAlways))

	first := publishAndGetInteractionID(t, router, store, threadID, "ember-owl", nil)
	require.NotEmpty(t, first)

	now = now.Add(2 * time.Hour)
	second := publishAndGetInteractionID(t, router, store, threadID, "iron-fox", nil)
	assert.Equal(t, first, second, "a thread is the interaction — no idle rotation")
}

// TestInteractionResolver_ZeroTimeoutDisablesRotation — IP3: an explicit 0
// window means idle rotation off for that channel.
func TestInteractionResolver_ZeroTimeoutDisablesRotation(t *testing.T) {
	router, store, ch, now := resolverHarness(t)
	router.SetInteractionIdleTimeout(ch, 0)

	first := publishAndGetInteractionID(t, router, store, ch, "ember-owl", nil)
	*now = now.Add(24 * time.Hour)
	second := publishAndGetInteractionID(t, router, store, ch, "iron-fox", nil)
	assert.Equal(t, first, second, "a zero idle window disables rotation")
}

// TestInteractionResolver_PerChannelTimeoutHonoured — the per-channel knob
// overrides the 600s default in both directions.
func TestInteractionResolver_PerChannelTimeoutHonoured(t *testing.T) {
	router, store, ch, now := resolverHarness(t)
	router.SetInteractionIdleTimeout(ch, 60)

	first := publishAndGetInteractionID(t, router, store, ch, "ember-owl", nil)
	*now = now.Add(61 * time.Second)
	second := publishAndGetInteractionID(t, router, store, ch, "iron-fox", nil)
	assert.NotEqual(t, first, second, "a shortened per-channel window rotates sooner than the default")
}

// TestInteractionResolver_OverridesInboundClaim — IP2: the router's resolution
// is authoritative. A publisher-supplied interaction_id is replaced on the
// persisted row and never keys governance state.
func TestInteractionResolver_OverridesInboundClaim(t *testing.T) {
	router, store, ch, _ := resolverHarness(t)
	router.SetReplyBudget(ch, 5)

	got := publishAndGetInteractionID(t, router, store, ch, "ember-owl",
		map[string]any{interactionIDMetadataKey: "spoofed-claim"})
	require.NotEmpty(t, got)
	assert.NotEqual(t, "spoofed-claim", got, "the resolver overrides the inbound claim")

	router.replyBudgetMu.Lock()
	_, spoofKeyed := router.replyCounts["spoofed-claim"]
	_, resolvedKeyed := router.replyCounts[got]
	router.replyBudgetMu.Unlock()
	assert.False(t, spoofKeyed, "a spoofed claim never keys a governance map")
	assert.True(t, resolvedKeyed, "the resolved id keys the reply-budget state")
}

// TestInteractionResolver_VoteCloseMintsFresh — IP8: the Layer 4 quorum close
// notifies the resolver, so the very next publish mints a fresh interaction
// instead of stamping the closed id into post-close suppression for the rest
// of the idle window. The closed id's tombstone survives until its deferred
// discard (IP4) and is then pruned — no lifetime entry.
func TestInteractionResolver_VoteCloseMintsFresh(t *testing.T) {
	router, store, ch, now := resolverHarness(t)
	router.SetEndVoteParams(ch, 2, 3)

	first := publishAndGetInteractionID(t, router, store, ch, "ember-owl", nil)

	// Two distinct voters reach quorum (K=2) — the second vote closes.
	for _, voter := range []string{"ember-owl", "iron-fox"} {
		require.NoError(t, router.Publish(context.Background(), ChannelMessage{
			ID: uuid.NewString(), ChannelID: ch, SenderID: voter, Content: "done",
			Metadata: map[string]any{endVoteMetadataKey: true},
		}, ""))
	}
	router.endVoteMu.Lock()
	_, tombstoned := router.closedInteractions[first]
	router.endVoteMu.Unlock()
	require.True(t, tombstoned, "the quorum close leaves the suppression tombstone")

	next := publishAndGetInteractionID(t, router, store, ch, "ember-owl", nil)
	require.NotEmpty(t, next)
	assert.NotEqual(t, first, next,
		"the publish after a vote-close mints fresh — quorum ends one conversation, not the channel")

	// The tombstone lives until the closed id's deferred discard: rotate twice.
	*now = now.Add(601 * time.Second)
	publishAndGetInteractionID(t, router, store, ch, "iron-fox", nil)
	*now = now.Add(601 * time.Second)
	publishAndGetInteractionID(t, router, store, ch, "ember-owl", nil)

	router.endVoteMu.Lock()
	_, tombstoned = router.closedInteractions[first]
	router.endVoteMu.Unlock()
	assert.False(t, tombstoned, "the vote-closed id's tombstone is eventually pruned — no lifetime entry")
}

// TestInteractionResolver_RaceRecreatedCounterSelfHealed — IP4/IP8: a commit
// that resolved the closing id just before the quorum landed re-enters via
// processEndVote after the close. The still-live tombstone suppresses it and
// the landed post-close self-heal re-prunes the counter its budget reservation
// recreated.
func TestInteractionResolver_RaceRecreatedCounterSelfHealed(t *testing.T) {
	router, store, ch, _ := resolverHarness(t)
	router.SetEndVoteParams(ch, 2, 3)
	router.SetReplyBudget(ch, 5)

	first := publishAndGetInteractionID(t, router, store, ch, "ember-owl", nil)
	for _, voter := range []string{"ember-owl", "iron-fox"} {
		require.NoError(t, router.Publish(context.Background(), ChannelMessage{
			ID: uuid.NewString(), ChannelID: ch, SenderID: voter, Content: "done",
			Metadata: map[string]any{endVoteMetadataKey: true},
		}, ""))
	}

	// The racing commit's tail: its budget reservation recreated the counter…
	router.replyBudgetMu.Lock()
	router.replyCounts[first] = map[string]int{"iron-fox": 1}
	router.replyBudgetMu.Unlock()
	// …and its end-vote hook now runs against the closed interaction.
	suppressed := router.processEndVote(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: ch, SenderID: "iron-fox", Content: "racing",
		Metadata: map[string]any{interactionIDMetadataKey: first},
	}, ChannelTypeGroup)

	assert.True(t, suppressed, "the racing commit's fanout is suppressed by the still-live tombstone")
	router.replyBudgetMu.Lock()
	_, recreated := router.replyCounts[first]
	router.replyBudgetMu.Unlock()
	assert.False(t, recreated, "the recreated counter is self-healed by the post-close re-discard")
}
