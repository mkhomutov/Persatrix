package channels

// bounded_close_stale_test.go — the PR #716 review staleness-discrimination
// pins, split out of bounded_close_test.go for the 500-line review cap. The
// review confirmed two losses in the round-7 stale shape: (a) EVERY
// divergence mapped to the withhold, so a benign resolver interleaving
// silently swallowed a live committed message; (b) a bound-crossing fanout
// that lost the tombstone CAS reported `closed` and its message vanished from
// every member's record without a trace. The fixed contract: only a
// DELIBERATE close (the no-reopen ledger, or losing the closing race to a
// standing tombstone) withholds; artefact divergences dispatch as pre-4b-i.

import (
	"context"
	"testing"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestBoundedClose_ArtifactDivergenceStillDispatches — the benign-interleaving
// pin, the counterpart of bounded_close_test.go's racing-sibling withhold: a
// fanout whose stamp diverges from the open id WITHOUT a deliberate close must
// dispatch. The resolver produces exactly this shape via its orphan-park
// interleaving (settleInteraction: a sibling rejected publish deletes the
// shared tentative mint and a third publish remints before the orphan's settle
// runs), which its own doc calls "an interleaving artefact, not a close" — no
// close fired, the successor is live, and the orphan's committed message is a
// real turn every member must still receive. Discriminating: the round-7 shape
// mapped EVERY divergence to the stale withhold, so the orphan's persisted
// message reached no member, live or otherwise, on an interaction that never
// terminated. Only a deliberate close may withhold.
func TestBoundedClose_ArtifactDivergenceStillDispatches(t *testing.T) {
	router, disp, ch, reader := concurrentCloseHarness(t, 100) // bound out of reach

	tick(t, router, ch) // opens interaction X, committed
	router.WaitForPendingFanout()
	before := liveDispatches(disp)

	// Reproduce the settled orphan-park state directly on the resolver entry
	// (the interleaving itself has no deterministic seam through Publish —
	// the stale-sibling test drives its tail the same way): X parks as the
	// retiree slot's ARTEFACT occupant, a fresh successor Y is open and
	// committed, and X never enters the no-reopen ledger (no close happened).
	router.interactionMu.Lock()
	entry := router.openInteractions[ch]
	orphanID := entry.id
	entry.retired = orphanID
	entry.id = uuid.NewString()
	entry.idCommitted = true
	router.interactionMu.Unlock()

	// The orphan's detached fanout tail runs now, stamped with X.
	router.fanout(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: ch, SenderID: "operator", Content: "orphaned but live",
		Metadata: map[string]any{interactionIDMetadataKey: orphanID},
	}, ChannelTypeGroup, "")

	assert.Equal(t, before+1, liveDispatches(disp),
		"an orphan-park divergence is an interleaving artefact, not a close — the committed message still reaches the roster")
	assert.Zero(t, closedCount(t, reader, structuralTrigger), "no close fired")
	assert.Zero(t, closedCount(t, reader, costTrigger), "no close fired")
	_, _, tracked := router.openInteractionEscalationState(ch)
	assert.True(t, tracked, "the successor interaction stays open")
}

// TestBoundedClose_CASLosingBoundCrosserWithholdsWithoutTeardown — the
// racing-closer pin: a bound-crossing fanout that finds the tombstone already
// standing (a sibling's close won the CAS, its teardown possibly still
// mid-flight) must neither dispatch its stimulus nor run any teardown of its
// own — and it reports the STALE shape, not `closed`, so the withhold is the
// logged outlived-sibling seam (which also skips the revival tails) rather
// than a silent masquerade as the close. The tombstone is planted directly
// between the tally advance and the tail, the winner-mid-teardown state no
// Publish interleaving reaches deterministically.
func TestBoundedClose_CASLosingBoundCrosserWithholdsWithoutTeardown(t *testing.T) {
	router, disp, ch, reader := concurrentCloseHarness(t, 2)

	tick(t, router, ch) // round 1: live, opens interaction X
	router.WaitForPendingFanout()
	boundID, _, tracked := router.openInteractionEscalationState(ch)
	require.True(t, tracked)
	before := liveDispatches(disp)

	// The racing winner's tombstone, mid-teardown (id not yet retired).
	router.endVoteMu.Lock()
	require.True(t, router.tombstoneInteractionLocked(boundID))
	router.endVoteMu.Unlock()

	// The loser's tail: crosses the bound (round 2 == max_rounds), loses the CAS.
	router.fanout(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: ch, SenderID: "operator", Content: "lost the closing race",
		Metadata: map[string]any{interactionIDMetadataKey: boundID},
	}, ChannelTypeGroup, "")

	assert.Equal(t, before, liveDispatches(disp),
		"the CAS loser's stimulus is withheld — its discussion is closing under the winner's hand")
	assert.Zero(t, closedCount(t, reader, structuralTrigger),
		"the loser runs no teardown and records no close of its own")
	assert.Empty(t, router.ChannelActivity(ch),
		"the withheld dispatch clears its head marks like every withhold")
}
