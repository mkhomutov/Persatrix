package channels

// ISSUE-0082 Part 2 PR 2 (v0.3.14) — the synthesis-close context reset.
//
// The plan's propagation lock covers descent from the detached request
// context: fanout children, chair escalations, close notifications and
// convene/synthesis dispatches all inherit it via `context.WithoutCancel`,
// which preserves values. [ChannelRouter.onSynthesisTimeout] was the one
// exception in the package — it runs on a `time.AfterFunc` goroutine and used
// to hand a bare `context.Background()` to [ChannelRouter.boundedClose].
//
// Principal is the ONLY scope axis such a reset exposes (session re-resolves
// through the SessionResolver, epoch falls back to the boot value), so the
// symptom was narrow and silent: an authenticated person's bounded discussion
// would have every turn partitioned EXCEPT the close-notification fan — the
// members' final turn, the one the RFC 0020 metered summary is built from —
// which would land in the shared `'local'` tenant. v0.3.14 PR 2 fixed it by
// stashing the arming request's principal on the pending entry and re-stamping
// the timer's context with it.
//
// ISSUE-0082 residuals PR 4b (v0.3.15) RETIRED that stash, and these tests are
// now the pin on the retirement rather than on the stash. The
// `(principal, speaker, scope)` re-key moved the tenant onto the RECORD: the
// fan closes `records_for_scope` principal-blind and each record re-binds its
// own frozen principal for its whole derivation, so the fan's ambient tenant
// selects nothing and every close path is free to disagree about it. The
// contract that survives is the one that was always the safety half — a
// principal-less interaction must never ACQUIRE one — and it now holds on both
// paths rather than one, which is why the two tests below assert the same
// thing about different origins.

import (
	"context"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// tickAs is [tick] with a principal on the publishing context — the shape a
// console publish by an authenticated account reaches the router with.
func tickAs(t *testing.T, router *ChannelRouter, ch, principal string) {
	t.Helper()
	ctx := WithPrincipal(context.Background(), principal)
	require.NoError(t, router.Publish(ctx, ChannelMessage{
		ID: uuid.NewString(), ChannelID: ch, SenderID: "operator", Content: "continue",
	}, ""))
}

// TestSynthesisClose_TimeoutFanCarriesNoPrincipalAfterRetirement is the
// inverted regression. The chair never replies, the timeout net closes on a
// context it constructed, and that context now carries NO principal — the
// v0.3.14 stash is gone (PR 4b).
//
// What makes the drop safe is not asserted here, because it is not observable
// here: the tenant of the derived rows is chosen agent-side off
// `Interaction.principal_id`, frozen at open, and re-bound by
// `record_write_scopes` for the whole close derivation. The Go-side facts this
// test CAN carry are that the notification stops naming a tenant and that the
// synthesis directive — dispatched inline from the arming fanout, so it never
// depended on the stash — still does. Asserting the directive is the half that
// earns its keep: a "cleanup" that reached past the timer path and stripped the
// arming fanout's own principal would break the ordinary per-turn boundary
// v0.3.14 shipped, and would otherwise look identical to this change.
func TestSynthesisClose_TimeoutFanCarriesNoPrincipalAfterRetirement(t *testing.T) {
	router, disp, ch, _ := synthesisCloseHarness(t, 2)
	router.synthesisTimeout = 5 * time.Millisecond

	tickAs(t, router, ch, "alice-participant")
	tickAs(t, router, ch, "alice-participant") // bound → synthesis armed, timer running

	require.Eventually(t, func() bool {
		return len(disp.closeNotifications()) > 0
	}, 2*time.Second, 2*time.Millisecond, "the timeout net closes and fans the notification")

	for _, c := range disp.closeNotifications() {
		assert.Empty(t, c.principal,
			"the timeout net's close fan no longer re-stamps the arming principal — the "+
				"record binds its own tenant for the derivation (PR 4b), so the fan selects "+
				"nothing by tenant and the stash was retired")
	}
	turns := disp.synthesisTurns()
	require.NotEmpty(t, turns)
	for _, c := range turns {
		assert.Equal(t, "alice-participant", c.principal,
			"the synthesis directive still rides the arming request's principal: it is "+
				"dispatched inline from that request's own fanout, and retiring the timer-path "+
				"stash must not reach the ordinary per-turn emission rail")
	}
}

// TestSynthesisClose_TimeoutFanEmitsNoPrincipalWhenUnarmed is the half that did
// not move: an agent/autonomous-origin discussion (or any turn under
// `auth.mode: disabled`) arms with no principal, so nothing on the arc may
// invent one. It survives the PR 4b retirement unchanged.
//
// What separates it from the test above is the ORIGIN, not the timer path — and
// that distinction changed shape with the inversion, so state it exactly. Both
// tests now assert the close fan names no tenant, so a change that re-stamped a
// DEFAULT tenant there fails both, not just this one. What only this fixture can
// see is a fabrication on the rail the other one legitimately expects a
// principal on: with no principal presented at arm time, the synthesis
// DIRECTIVE must be empty too. A change that defaulted the arming context to
// `local`, or lifted an unrelated agent's ISSUE-0124 attribution onto it, would
// look indistinguishable from correct propagation in the test above — whose
// fixture supplies a real principal for the directive to carry — and is caught
// only here. Hence the assertion over EVERY dispatch, directive included.
func TestSynthesisClose_TimeoutFanEmitsNoPrincipalWhenUnarmed(t *testing.T) {
	router, disp, ch, _ := synthesisCloseHarness(t, 2)
	router.synthesisTimeout = 5 * time.Millisecond

	tick(t, router, ch)
	tick(t, router, ch)

	require.Eventually(t, func() bool {
		return len(disp.closeNotifications()) > 0
	}, 2*time.Second, 2*time.Millisecond, "the timeout net closes and fans the notification")

	// The directive half, asserted explicitly rather than left implicit in the
	// sweep below: it is the one the sibling test cannot make, and an empty
	// `synthesisTurns()` would satisfy a bare range loop while proving nothing.
	turns := disp.synthesisTurns()
	require.NotEmpty(t, turns, "the arm dispatched its directive")
	for _, c := range turns {
		assert.Empty(t, c.principal,
			"an unauthenticated origin's synthesis directive must not acquire a tenant — "+
				"a defaulted `local` here reads identically to correct propagation in "+
				"TestSynthesisClose_TimeoutFanCarriesNoPrincipalAfterRetirement")
	}
	for _, c := range disp.snapshot() {
		assert.Empty(t, c.principal,
			"a principal-less interaction must never acquire one anywhere on the arc")
	}
}
