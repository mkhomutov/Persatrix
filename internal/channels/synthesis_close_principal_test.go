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
// which would land in the shared `'local'` tenant. These tests pin the fix at
// both ends: the principal survives the timer hop, and no principal stays no
// principal.

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

// TestSynthesisClose_TimeoutFanCarriesArmingPrincipal is the regression: the
// chair never replies, the timeout net closes on a context it constructed, and
// the close-notification fan must still be tagged with the principal of the
// person whose publish crossed the bound.
func TestSynthesisClose_TimeoutFanCarriesArmingPrincipal(t *testing.T) {
	router, disp, ch, _ := synthesisCloseHarness(t, 2)
	router.synthesisTimeout = 5 * time.Millisecond

	tickAs(t, router, ch, "alice-participant")
	tickAs(t, router, ch, "alice-participant") // bound → synthesis armed, timer running

	require.Eventually(t, func() bool {
		return len(disp.closeNotifications()) > 0
	}, 2*time.Second, 2*time.Millisecond, "the timeout net closes and fans the notification")

	for _, c := range disp.closeNotifications() {
		assert.Equal(t, "alice-participant", c.principal,
			"the close-notification fan must stay in the arming request's tenant — "+
				"the timer goroutine owns no request, so the principal has to be stashed at arm time")
	}
	// The synthesis directive itself is dispatched inline from the arming
	// fanout, so it never depended on the stash — assert it anyway, because a
	// fix that moved the principal onto the timer path while dropping it from
	// the arm path would still split one interaction across two tenants.
	turns := disp.synthesisTurns()
	require.NotEmpty(t, turns)
	for _, c := range turns {
		assert.Equal(t, "alice-participant", c.principal,
			"the synthesis directive rides the arming request's principal too")
	}
}

// TestSynthesisClose_TimeoutFanEmitsNoPrincipalWhenUnarmed is the no-delta
// half: an agent/autonomous-origin discussion (or any turn under `auth.mode:
// disabled`) arms with no principal, so the timeout close must reach the
// dispatcher with none — byte-identical to a bare `context.Background()`, the
// pre-v0.3.14 behaviour.
func TestSynthesisClose_TimeoutFanEmitsNoPrincipalWhenUnarmed(t *testing.T) {
	router, disp, ch, _ := synthesisCloseHarness(t, 2)
	router.synthesisTimeout = 5 * time.Millisecond

	tick(t, router, ch)
	tick(t, router, ch)

	require.Eventually(t, func() bool {
		return len(disp.closeNotifications()) > 0
	}, 2*time.Second, 2*time.Millisecond, "the timeout net closes and fans the notification")

	for _, c := range disp.snapshot() {
		assert.Empty(t, c.principal,
			"a principal-less interaction must never acquire one on the timeout path")
	}
}
