package channels

import (
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestInteractionBudgetSnapshot_OnCommitAndDeferredEvict pins the RFC 0050
// amendment lifecycle: a capped channel's interaction is snapshotted when it
// first commits (so the wallet resolver can read it), the snapshot is fixed for
// the interaction's life, and it is evicted one generation after close — the same
// deferred discharge as the end-vote tombstone, so a lease racing the close still
// resolves the ceiling.
func TestInteractionBudgetSnapshot_OnCommitAndDeferredEvict(t *testing.T) {
	router, store, ch, now := resolverHarness(t)
	router.SetInteractionBudgetTokens(ch, 4096)

	first := publishAndGetMetadata(t, router, store, ch, "ember-owl", nil)
	firstID, _ := first[interactionIDMetadataKey].(string)
	require.NotEmpty(t, firstID)

	v, ok := router.ResolveInteractionBudgetForInteraction(firstID)
	require.True(t, ok, "a capped channel's interaction is snapshotted on commit")
	assert.EqualValues(t, 4096, v)

	// One idle rotation: firstID becomes the pending retiree. Its snapshot
	// survives the post-close suppression window (one-generation deferral).
	*now = now.Add(601 * time.Second)
	publishAndGetMetadata(t, router, store, ch, "iron-fox", nil)
	_, ok = router.ResolveInteractionBudgetForInteraction(firstID)
	assert.True(t, ok, "snapshot survives the first rotation (deferred, like the end-vote tombstone)")

	// A second rotation discharges the parked retiree → firstID evicted.
	*now = now.Add(601 * time.Second)
	publishAndGetMetadata(t, router, store, ch, "ember-owl", nil)
	_, ok = router.ResolveInteractionBudgetForInteraction(firstID)
	assert.False(t, ok, "snapshot evicted one generation after close")
}

// TestInteractionBudgetSnapshot_UncappedRecordsNoEntry proves the latent-until-
// configured property: an uncapped channel (budget 0) records no snapshot, so the
// resolver misses and the wallet treats the lease as uncapped — interactionBudgetSnapshots
// stays empty on a fleet with no ceilings set.
func TestInteractionBudgetSnapshot_UncappedRecordsNoEntry(t *testing.T) {
	router, store, ch, _ := resolverHarness(t)
	// channel left uncapped (budget 0)

	m := publishAndGetMetadata(t, router, store, ch, "ember-owl", nil)
	iid, _ := m[interactionIDMetadataKey].(string)
	require.NotEmpty(t, iid)

	_, ok := router.ResolveInteractionBudgetForInteraction(iid)
	assert.False(t, ok, "an uncapped channel records no snapshot — resolver miss = uncapped")
}

// TestInteractionBudgetSnapshot_DiscardIsIdempotent pins the eviction primitive:
// discarding an unknown or already-evicted interaction is a harmless no-op.
func TestInteractionBudgetSnapshot_DiscardIsIdempotent(t *testing.T) {
	router, _, _ := newRouterTest(t)
	router.SetInteractionBudgetTokens("group:x", 1000)
	router.snapshotInteractionBudget("int-1", "group:x")

	router.DiscardInteractionBudget("int-1")
	router.DiscardInteractionBudget("int-1") // second discard is a no-op
	router.DiscardInteractionBudget("never-seen")
	_, ok := router.ResolveInteractionBudgetForInteraction("int-1")
	assert.False(t, ok)
}
