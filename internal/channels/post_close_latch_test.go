package channels

// post_close_latch_test.go — the RFC 0052 no-reopen latch (PR 4b-i review
// round 5), the publish-path half of the bounded close's no-reopen guarantee.
// The fanout-tail guards (bounded_close_test.go) stop the CLOSING fanout from
// reviving its own interaction; this latch stops the traffic those guards
// cannot see — a floor straggler whose reply lands after its bounding round
// ended, or a reply drawn by a sub-bound sibling fanout that raced the close
// on the concurrent path. Both re-enter Publish CLAIMING the retired id;
// pre-latch, IP2 overrode the claim and minted fresh, re-fanning the reply and
// reopening the unattended discussion (the §D runaway). The latch keeps the
// claim so the publish rides processEndVote's tombstone branch instead:
// persisted, fanout suppressed, no mint. Scoped to `autonomous.enabled` —
// human-channel post-close traffic keeps minting fresh, byte-for-byte
// unchanged.

import (
	"context"
	"testing"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
)

// liveDispatches counts the recorder's ordinary (non-close-notification)
// envelopes — the deliveries that draw replies and could reopen a discussion.
func liveDispatches(disp *envelopeRecorder) int {
	n := 0
	for _, env := range disp.snapshot() {
		if !env.InteractionCloseNotification {
			n++
		}
	}
	return n
}

// TestPostCloseLatch_StragglerClaimDoesNotReopen — the latch's headline: after
// a bounded close retires the id, a reply claiming that id (an agent echoes
// the id it was dispatched under) is persisted WITHOUT minting a fresh
// interaction and draws no fanout — the discussion stays terminated. Pre-latch
// this publish minted fresh and re-fanned: the reopen the deep review's
// fanout-tail guards could not reach, because the straggler arrives on its own
// publish, outside the closing fanout's call frame.
func TestPostCloseLatch_StragglerClaimDoesNotReopen(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	disp := &envelopeRecorder{}
	router := NewChannelRouter(store, disp, zap.NewNop(), nil)
	// Floor control OFF → the concurrent path, whose dispatched replies
	// re-enter Publish — the racing-sibling shape.
	ch := mustCreateGroupWithPolicies(t, store, "brainstorm",
		map[string]RespondPolicy{
			"operator":  RespondNever,
			"ember-owl": RespondAlways,
		}, "operator", "ember-owl")
	router.SetAutonomous(ch, AutonomousConfig{Enabled: true, MaxRounds: 2, Convener: "ember-owl"})

	tick(t, router, ch) // round 1: dispatched live
	closedID, _, tracked := router.openInteractionEscalationState(ch)
	require.True(t, tracked, "round 1 opens the interaction the straggler will claim")
	tick(t, router, ch) // round 2 == max_rounds → bounded close retires the id
	router.WaitForPendingFanout()
	_, _, tracked = router.openInteractionEscalationState(ch)
	require.False(t, tracked, "the bounded close retired the id")
	before := liveDispatches(disp)

	// The straggler: ember-owl's late reply to the round-1 stimulus, claiming
	// the interaction it was dispatched under. Commits fine (nil error) — the
	// closed record's late final word — but must neither mint nor fan.
	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: ch, SenderID: "ember-owl", Content: "late thought",
		Metadata: map[string]any{interactionIDMetadataKey: closedID},
	}, ""))
	router.WaitForPendingFanout()

	_, _, tracked = router.openInteractionEscalationState(ch)
	assert.False(t, tracked,
		"a post-close claim must not mint a fresh interaction — that mint IS the reopen")
	assert.Equal(t, before, liveDispatches(disp), "the latched straggler draws no fanout")

	// IP8 stands: the latch suppresses only traffic CLAIMING the closed id. An
	// unstamped publish (the operator, a convener's opening turn) still mints
	// fresh, so the channel remains re-convenable.
	tick(t, router, ch)
	_, _, tracked = router.openInteractionEscalationState(ch)
	assert.True(t, tracked, "an unstamped publish still mints fresh — the channel stays re-convenable")
}

// TestPostCloseLatch_ForeignChannelTombstoneNeverLatches — the channel-scoping
// pin: the tombstone map is keyed by interaction id alone, so without the
// retiree comparison a member of autonomous channel B could stamp channel A's
// tombstoned id onto its publish and have it persist under an interaction that
// never existed on B (its own fanout suppressed — self-harm, but polluted
// attribution). The latch must fire only for THIS channel's pending retiree; a
// foreign claim keeps the IP2 override and mints fresh like any stale claim.
func TestPostCloseLatch_ForeignChannelTombstoneNeverLatches(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	disp := &envelopeRecorder{}
	router := NewChannelRouter(store, disp, zap.NewNop(), nil)
	chA := mustCreateGroupWithPolicies(t, store, "brainstorm",
		map[string]RespondPolicy{
			"operator":  RespondNever,
			"ember-owl": RespondAlways,
		}, "operator", "ember-owl")
	chB := mustCreateGroupWithPolicies(t, store, "planning",
		map[string]RespondPolicy{
			"operator":  RespondNever,
			"ember-owl": RespondAlways,
		}, "operator", "ember-owl")
	router.SetAutonomous(chA, AutonomousConfig{Enabled: true, MaxRounds: 1, Convener: "ember-owl"})
	router.SetAutonomous(chB, AutonomousConfig{Enabled: true, MaxRounds: 100, Convener: "ember-owl"})

	// Bound-close channel A on its opening turn; its id is now tombstoned.
	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: chA, SenderID: "operator", Content: "go",
	}, ""))
	router.WaitForPendingFanout()
	foreignID := router.retiredInteractionFor(chA)
	require.NotEmpty(t, foreignID, "channel A's bounded close parked its id as the retiree")
	require.True(t, router.interactionTombstoned(foreignID))

	// A publish on channel B claiming channel A's tombstoned id: NOT B's
	// retiree, so the latch must not fire — IP2 overrides and mints fresh.
	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: chB, SenderID: "ember-owl", Content: "hello",
		Metadata: map[string]any{interactionIDMetadataKey: foreignID},
	}, ""))
	router.WaitForPendingFanout()

	openB, _, tracked := router.openInteractionEscalationState(chB)
	assert.True(t, tracked, "the foreign claim keeps the IP2 override — channel B mints its own interaction")
	assert.NotEqual(t, foreignID, openB, "and never adopts the foreign id")
}

// TestPostCloseLatch_HumanChannelClaimStillMintsFresh — the OQ #2 scope pin:
// the latch is autonomous-only. On a human channel, a post-close publish
// claiming the end-vote-closed id keeps the shipped IP2 behaviour — the claim
// is overridden, a fresh interaction is minted, and the message fans out —
// byte-for-byte unchanged.
func TestPostCloseLatch_HumanChannelClaimStillMintsFresh(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	disp := &envelopeRecorder{}
	router := NewChannelRouter(store, disp, zap.NewNop(), nil)
	ch := mustCreateGroupWithPolicies(t, store, "planning",
		map[string]RespondPolicy{
			"alice": RespondAlways,
			"bob":   RespondAlways,
		}, "alice", "bob")
	// Autonomous stays disabled: an ordinary human channel.

	// Open the interaction and close it by the real Layer 4 quorum (K=2).
	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: ch, SenderID: "alice", Content: "done, I think",
		Metadata: map[string]any{endVoteMetadataKey: true},
	}, ""))
	closedID, _, tracked := router.openInteractionEscalationState(ch)
	require.True(t, tracked)
	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: ch, SenderID: "bob", Content: "agreed, closing",
		Metadata: map[string]any{endVoteMetadataKey: true},
	}, ""))
	router.WaitForPendingFanout()
	_, _, tracked = router.openInteractionEscalationState(ch)
	require.False(t, tracked, "the end-vote quorum closed and retired the id")
	before := liveDispatches(disp)

	// A late reply claiming the closed id: on a HUMAN channel the tombstoned
	// claim must keep minting fresh and fanning out — the latch never fires.
	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: ch, SenderID: "alice", Content: "one more thing",
		Metadata: map[string]any{interactionIDMetadataKey: closedID},
	}, ""))
	router.WaitForPendingFanout()

	_, _, tracked = router.openInteractionEscalationState(ch)
	assert.True(t, tracked, "human-channel post-close traffic still mints fresh (IP2 unchanged)")
	assert.Greater(t, liveDispatches(disp), before, "and still fans out")
}
