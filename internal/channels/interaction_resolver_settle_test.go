package channels

// interaction_resolver_settle_test.go — the settle/reconciliation half of the
// RFC 0030 interaction-id producer (interaction_resolver.go), split from
// interaction_resolver_test.go for the 500-line cap. These tests pin
// [ChannelRouter.settleInteraction]'s orphaned-commit handling: the
// three-way interleave where a persisted publish's stamped id is no longer
// the entry's open id by the time its settle hook runs.

import (
	"context"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestInteractionResolver_OrphanedCommitParksAsRetiree — the orphan
// interleave: P (persist fails) and Q (persist succeeds) race on a tentative
// mint; P's settle deletes the shared entry, a third publish remints before
// Q's settle runs, so Q's persisted row is stamped with an id that is neither
// the open id nor the pending retiree. Without parking it, the id's committed
// governance state (Q's reply-budget reservation) is off the record forever —
// no rotation or close ever discharges it: a lifetime counter map. The settle
// must park the orphan as the pending retiree so the channel's next rotation
// discharges it like any retiree.
func TestInteractionResolver_OrphanedCommitParksAsRetiree(t *testing.T) {
	router, store, ch, now := resolverHarness(t)
	router.SetReplyBudget(ch, 5)
	ctx := context.Background()

	// P and Q race on the channel's first publish: both resolve the same
	// tentative mint (publishCommit calls resolve before the store persist,
	// so two in-flight publishes share it).
	_, _, settleP, _ := router.resolveInteractionID(ctx, ch, ChannelTypeGroup, "", false)
	orphan, _, settleQ, _ := router.resolveInteractionID(ctx, ch, ChannelTypeGroup, "", false)

	// P's persist fails and settles first: the sole-tentative entry is
	// deleted (the rejected-publish bound). R publishes before Q settles,
	// reminting a fresh open id.
	settleP(false)
	reminted := publishAndGetInteractionID(t, router, store, ch, "ember-owl", nil)
	require.NotEqual(t, orphan, reminted, "R's publish remints — the orphan is off the record")

	// Q persisted under the orphan id; its committed reply-budget
	// reservation is the state at stake (the reservation releases only on a
	// FAILED persist — Q's succeeded).
	router.replyBudgetMu.Lock()
	router.replyCounts[orphan] = map[string]int{"iron-fox": 1}
	router.replyBudgetMu.Unlock()
	settleQ(true)

	router.interactionMu.Lock()
	parked := router.openInteractions[ch].retired
	router.interactionMu.Unlock()
	assert.Equal(t, orphan, parked,
		"the orphaned committed id is parked as the pending retiree")

	// The next rotation discharges it like any retiree — no lifetime entry.
	*now = now.Add(601 * time.Second)
	publishAndGetInteractionID(t, router, store, ch, "iron-fox", nil)
	router.replyBudgetMu.Lock()
	_, alive := router.replyCounts[orphan]
	router.replyBudgetMu.Unlock()
	assert.False(t, alive,
		"the orphan's reply-budget state is discharged at the channel's next rotation")
}

// TestInteractionResolver_SettleDoesNotClobberPendingRetiree — the guard on
// the parking fix: when the retiree slot is already occupied, a stale settle
// must NOT displace it. The occupant's one-generation-deferred discard is
// what protects a real commit racing the rotation/close (IP4); evicting it
// early recreates the very race the deferral exists to close. The stale id's
// own state is the accepted residue here — reaching this case requires one
// publish's persist to span an entire rotation cycle, so it is vanishingly
// rare and router-minted, not a growth vector.
func TestInteractionResolver_SettleDoesNotClobberPendingRetiree(t *testing.T) {
	router, store, ch, now := resolverHarness(t)

	first := publishAndGetInteractionID(t, router, store, ch, "ember-owl", nil)
	*now = now.Add(601 * time.Second)
	second := publishAndGetInteractionID(t, router, store, ch, "iron-fox", nil)
	require.NotEqual(t, first, second)
	// `first` is now the pending retiree; its deferred discard must survive.

	router.settleInteraction(ch, "orphan-spanning-a-rotation", router.interactionNow(), true)

	router.interactionMu.Lock()
	entry := router.openInteractions[ch]
	open, retired := entry.id, entry.retired
	router.interactionMu.Unlock()
	assert.Equal(t, first, retired,
		"an occupied retiree slot is never clobbered — the deferral guarantee wins")
	assert.Equal(t, second, open, "the open id is untouched by a stale settle")
}
