package channels

// post_close_latch_test.go — the RFC 0052 no-reopen latch (PR 4b-i review
// rounds 5–6), the publish-path half of the bounded close's no-reopen
// guarantee. The fanout-tail guards (bounded_close_test.go) stop the CLOSING
// fanout from reviving its own interaction; this latch stops the traffic
// those guards cannot see — a floor straggler whose reply lands after its
// bounding round ended, or a reply drawn by a sub-bound sibling fanout that
// raced the close on the concurrent path. Both re-enter Publish CLAIMING the
// retired id; pre-latch, IP2 overrode the claim and minted fresh, re-fanning
// the reply and reopening the unattended discussion (the §D runaway). The
// latch keeps the claim and publishCommit suppresses the fanout directly:
// persisted as the closed record's late final word, metered as the Layer 4
// governance drop, no mint. The decision lives INSIDE the resolver's critical
// section, keyed on the per-channel ledger of deliberately closed ids
// (interaction_close_latch.go). Scoped to `autonomous.enabled` —
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
	// The concurrent-path stage (bounded_close_test.go): dispatched replies
	// re-enter Publish — the racing-sibling shape.
	router, disp, ch, _ := concurrentCloseHarness(t, 2)

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

// TestPostCloseLatch_ForeignChannelCloseNeverLatches — the channel-scoping
// pin: the no-reopen ledger is per-channel by construction, so a member of
// autonomous channel B stamping channel A's closed id onto its publish must
// not have it persist under an interaction that never existed on B (its own
// fanout suppressed — self-harm, but polluted attribution). A foreign claim
// keeps the IP2 override and mints fresh like any stale claim.
func TestPostCloseLatch_ForeignChannelCloseNeverLatches(t *testing.T) {
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
	router.SetAutonomous(chA, AutonomousConfig{Enabled: true, MaxRounds: 2, Convener: "ember-owl"})
	router.SetAutonomous(chB, AutonomousConfig{Enabled: true, MaxRounds: 100, Convener: "ember-owl"})

	// Bound-close channel A: round 1 opens (and is dispatched live — the §D
	// round-1 guard), round 2 crosses the bound and closes.
	tick(t, router, chA)
	foreignID, _, tracked := router.openInteractionEscalationState(chA)
	require.True(t, tracked, "channel A's round 1 opens the interaction")
	tick(t, router, chA)
	router.WaitForPendingFanout()
	_, _, tracked = router.openInteractionEscalationState(chA)
	require.False(t, tracked, "channel A's bounded close retired its id")

	// A publish on channel B claiming channel A's closed id: not on B's
	// ledger, so the latch must not fire — IP2 overrides and mints fresh.
	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: chB, SenderID: "ember-owl", Content: "hello",
		Metadata: map[string]any{interactionIDMetadataKey: foreignID},
	}, ""))
	router.WaitForPendingFanout()

	openB, _, tracked := router.openInteractionEscalationState(chB)
	assert.True(t, tracked, "the foreign claim keeps the IP2 override — channel B mints its own interaction")
	assert.NotEqual(t, foreignID, openB, "and never adopts the foreign id")
}

// TestPostCloseLatch_SurvivesSuccessorGenerations — the PR #716 review
// one-generation escape: the round-5 latch keyed on the channel's single
// retiree slot plus the tombstone, BOTH displaced when the next generation
// closes (markInteractionClosed discharges the previous retiree's tombstone).
// A very late straggler claiming generation A after close(A) → re-convene →
// close(B) escaped both conjuncts, fell into IP2, minted fresh, and re-fanned
// — reopening the unattended channel. The ledger spans generations, so the
// claim latches regardless of how many closes landed since.
func TestPostCloseLatch_SurvivesSuccessorGenerations(t *testing.T) {
	router, disp, ch, _ := concurrentCloseHarness(t, 2)

	// Generation A: opens on round 1, bounded-closes on round 2.
	tick(t, router, ch)
	closedA, _, tracked := router.openInteractionEscalationState(ch)
	require.True(t, tracked)
	tick(t, router, ch)
	router.WaitForPendingFanout()
	_, _, tracked = router.openInteractionEscalationState(ch)
	require.False(t, tracked, "generation A bounded-closed")

	// Generation B: re-convene and close AGAIN. B's close overwrites the
	// retiree slot and discharges A's tombstone — the exact state the pre-fix
	// latch keyed on.
	tick(t, router, ch)
	closedB, _, tracked := router.openInteractionEscalationState(ch)
	require.True(t, tracked, "the channel re-convenes into generation B")
	require.NotEqual(t, closedA, closedB)
	tick(t, router, ch)
	router.WaitForPendingFanout()
	_, _, tracked = router.openInteractionEscalationState(ch)
	require.False(t, tracked, "generation B bounded-closed")
	before := liveDispatches(disp)

	// The very late straggler: a reply claiming generation A, in flight since
	// before B ran (a slow LLM reply plus a fast re-convene compresses B's
	// whole lifetime inside one dispatch window). Must latch: no mint, no fan.
	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: ch, SenderID: "ember-owl", Content: "very late thought",
		Metadata: map[string]any{interactionIDMetadataKey: closedA},
	}, ""))
	router.WaitForPendingFanout()

	_, _, tracked = router.openInteractionEscalationState(ch)
	assert.False(t, tracked,
		"a straggler of a DISPLACED generation must still latch — its mint is the reopen")
	assert.Equal(t, before, liveDispatches(disp), "and it draws no fanout")
}

// TestPostCloseLatch_LatchDecisionIsInsideTheResolve — the PR #716 review
// TOCTOU pin, at the resolver seam: once markInteractionClosed has run, an
// autonomous channel's resolve of the closed claim returns the claim itself
// (latched, no mint, zero close-cause, no-op settle), because the decision and
// the would-be mint share one interactionMu critical section. The round-5
// shape — predicate on the publish path, resolve in a second acquisition —
// let a close land between the two and mint fresh for the very straggler the
// latch suppresses; with the decision inside the resolve there is no
// "between". The scope gate is likewise the RESOLVER's own (derived from the
// channel's autonomous config), so the human leg below drives the production
// disabled-default rather than a caller-supplied flag.
func TestPostCloseLatch_LatchDecisionIsInsideTheResolve(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	router := NewChannelRouter(store, &envelopeRecorder{}, zap.NewNop(), nil)
	ch := "group:atomic-latch"
	router.SetAutonomous(ch, AutonomousConfig{Enabled: true, Convener: "ember-owl"})
	ctx := context.Background()

	id, _, settle, latched := router.resolveInteractionID(ctx, ch, ChannelTypeGroup, "")
	require.False(t, latched)
	settle(true)
	router.markInteractionClosed(ch, id, structuralTrigger)

	// The straggler's resolve, an instant after the close.
	got, prev, _, nowLatched := router.resolveInteractionID(ctx, ch, ChannelTypeGroup, id)
	assert.True(t, nowLatched, "a deliberately closed claim latches inside the resolve")
	assert.Equal(t, id, got, "the latched resolve rides the closed id — no mint")
	assert.Empty(t, prev.id, "a latched publish carries no close-cause attribution (it is the closed record's tail)")

	// The same close-then-claim shape on a channel OUTSIDE the latch scope
	// (autonomous stays the disabled default — a human channel): the ledger is
	// written all the same, but the resolver's scope gate declines it and IP2
	// overrides and mints fresh, byte-for-byte unchanged.
	chHuman := "group:atomic-latch-human"
	idHuman, _, settleHuman, _ := router.resolveInteractionID(ctx, chHuman, ChannelTypeGroup, "")
	settleHuman(true)
	router.markInteractionClosed(chHuman, idHuman, structuralTrigger)
	fresh, _, settleFresh, humanLatched := router.resolveInteractionID(ctx, chHuman, ChannelTypeGroup, idHuman)
	assert.False(t, humanLatched)
	assert.NotEqual(t, idHuman, fresh, "outside the latch scope the closed claim still mints fresh (IP2)")
	settleFresh(false)
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

// TestPostCloseLatch_LatchedReplyStillNotifiesFloorWaiter — the PR #716 review
// carry-forward, made live by the round-8 publish-claim echo: a floor round's
// awaited speaker replies echoing the id it was dispatched under, and a
// mid-round deliberate close (an end-vote quorum on an earlier speaker, or a
// racing sibling's bounded close) has just retired that id — so the reply
// latches. The round's reply waiter (runFloorTurn, floor_control.go) is keyed
// on (channel, sender) and knows nothing of the close; a latch that returns
// BEFORE waiter.Notify starves it, burning the full turn timeout per remaining
// speaker on a terminated discussion and mislabeling each as
// floor_turn{timeout}. The reply IS persisted, so notifying is truthful —
// fanout suppression, not Notify starvation, is what prevents the reopen
// (Notify-then-suppress). The waiter is registered directly here, the round's
// own seam, because a real mid-round close cannot be interleaved
// deterministically through Publish.
func TestPostCloseLatch_LatchedReplyStillNotifiesFloorWaiter(t *testing.T) {
	router, disp, ch, _ := concurrentCloseHarness(t, 2)

	tick(t, router, ch) // round 1: opens the interaction, dispatched live
	closedID, _, tracked := router.openInteractionEscalationState(ch)
	require.True(t, tracked)
	tick(t, router, ch) // round 2 == max_rounds → the deliberate close retires the id
	router.WaitForPendingFanout()
	_, _, tracked = router.openInteractionEscalationState(ch)
	require.False(t, tracked, "the bounded close retired the id")
	before := liveDispatches(disp)

	// The mid-flight round awaits ember-owl's reply, exactly as runFloorTurn
	// registers it.
	replyCh, cancel, err := router.waiter.Register(ch, "ember-owl")
	require.NoError(t, err)
	defer cancel()

	// The awaited reply arrives echoing the now-closed id: it latches.
	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: ch, SenderID: "ember-owl", Content: "late reply",
		Metadata: map[string]any{interactionIDMetadataKey: closedID},
	}, ""))

	select {
	case <-replyCh:
		// Notified: the round advances on the reply instead of burning its
		// turn timeout against a discussion that already terminated.
	default:
		t.Fatal("a latched reply must still satisfy the floor round's reply waiter — fanout suppression, not Notify starvation, prevents the reopen")
	}
	router.WaitForPendingFanout()
	assert.Equal(t, before, liveDispatches(disp), "and the latched reply still draws no fanout")
}

// TestPostCloseLatch_FloorRoundWithheldWhenStimulusOutlivedClose — the floor
// path's fanout-HEAD ledger read (PR #716 review). The concurrent path's
// close-before-dispatch ordering withholds an outlived stimulus at the tail,
// BEFORE its dispatch; the floor round runs before that tail, so without the
// head check a deliberate close landing between a publish's commit and its
// detached fanout still dispatched a full multi-speaker round of LLM turns
// into the terminated discussion — replies the publish-path latch absorbed
// with the spend already spent. The fanout is invoked directly, standing in
// for the detached goroutine whose message committed before the close: a
// sequential Publish cannot reproduce the race, because a post-close claim
// latches at commit and never reaches fanout at all.
func TestPostCloseLatch_FloorRoundWithheldWhenStimulusOutlivedClose(t *testing.T) {
	router, disp, ch, _ := boundedCloseHarness(t, true, 10)

	tick(t, router, ch) // opens the interaction: one stalled floor round
	open, _, tracked := router.openInteractionEscalationState(ch)
	require.True(t, tracked)
	before := liveDispatches(disp)

	// The mid-flight deliberate close: an end-vote quorum (or a sibling's
	// bounded close) retires the id after the straggler's commit.
	router.markInteractionClosed(ch, open, endVotesTrigger)

	// The outlived fanout runs, its message stamped with the now-closed id.
	router.fanout(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: ch, SenderID: "operator", Content: "straggler",
		Metadata: map[string]any{interactionIDMetadataKey: open},
	}, ChannelTypeGroup, "")

	assert.Equal(t, before, liveDispatches(disp),
		"the floor round is withheld at the fanout head — running it would draw a full round of LLM replies into the terminated discussion")
}
