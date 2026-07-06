package channels

// bounded_close_redelivery_test.go — PR #718 follow-up review regressions on
// the bounded close's ACTION gate and its redelivery marker: a bound crossed
// under a stale enabled snapshot must not act once the operator disabled the
// block mid-round, and the floor path's `redelivery=true` must be resolved
// PER RECIPIENT — a member whose live dispatch of the bounding stimulus failed
// gets a sole-delivery notification, or the receiver's ingest-skip drops the
// closing turn from its record entirely.

import (
	"context"
	"errors"
	"fmt"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/registry"
)

// TestBoundedClose_DisableMidRoundLeavesInteractionOpen — the fanout-head
// autonomous snapshot can be minutes stale by the fanout tail (a full floor
// round sits between them). An RFC 0050 disable landing inside that round has
// already run its disarm — a no-op, nothing armed yet — so acting on the stale
// snapshot armed a synthesis close (and its enabled-blind timeout net then
// force-closed the now-manual discussion). The bound is only ever ACTED on
// against the CURRENT config: crossed tally, no action, interaction open under
// manual control. White-box at the tail trigger, because the disable must land
// inside the head-snapshot window deterministically.
func TestBoundedClose_DisableMidRoundLeavesInteractionOpen(t *testing.T) {
	router, disp, ch, reader := synthesisCloseHarness(t, 2)

	tick(t, router, ch) // round 1 on the enabled config
	stale := router.AutonomousFor(ch)
	require.True(t, stale.Enabled)
	members, err := router.store.GetMembers(context.Background(), ch)
	require.NoError(t, err)

	// The disable lands mid-round; the tail then runs with the head snapshot.
	router.SetAutonomous(ch, AutonomousConfig{Enabled: false})
	closed, staleVerdict := router.maybeBoundedClose(context.Background(),
		ChannelMessage{ID: uuid.NewString(), ChannelID: ch, SenderID: "operator", Content: "continue"},
		ChannelTypeGroup, members, len(members), false, nil, stale)

	assert.False(t, closed, "a disabled channel's crossed bound closes nothing")
	assert.False(t, staleVerdict, "…and withholds nothing — the message is live manual traffic")
	assert.Empty(t, disp.synthesisTurns(), "no synthesis turn is armed off the stale snapshot")
	assert.Zero(t, closedCount(t, reader, structuralTrigger))
	assert.Zero(t, closedCount(t, reader, costTrigger))
	_, _, tracked := router.openInteractionEscalationState(ch)
	assert.True(t, tracked, "the interaction stays open under the operator's manual control")
}

// TestBoundedClose_MaxRoundsRaiseMidRoundDefersClose — the enabled re-check's
// sibling (PR #718 follow-up review): the head snapshot can go stale on
// MaxRounds too. An RFC 0050 `max_rounds` RAISE landing inside the round used
// to be ignored — the tail's action-point re-check read only `fresh.Enabled`,
// so the extended discussion was closed (or armed) against the OLD bound. The
// bound decision is now re-derived from `fresh.MaxRounds` at the action
// point: crossed-on-stale-only acts as sub-bound, the tally survives, and the
// RAISED bound closes deterministically at its own round count.
func TestBoundedClose_MaxRoundsRaiseMidRoundDefersClose(t *testing.T) {
	router, disp, ch, reader := synthesisCloseHarness(t, 2)

	tick(t, router, ch) // round 1 on the old bound
	stale := router.AutonomousFor(ch)
	require.True(t, stale.Enabled)
	require.Equal(t, 2, stale.MaxRounds)
	members, err := router.store.GetMembers(context.Background(), ch)
	require.NoError(t, err)

	// The raise lands mid-round; the tail then runs with the head snapshot.
	router.SetAutonomous(ch, AutonomousConfig{
		Enabled: true, MaxRounds: 5, Convener: "ember-owl",
		Topic: "Adopt a monorepo?", Goal: "A synthesized recommendation.",
	})
	closed, staleVerdict := router.maybeBoundedClose(context.Background(),
		ChannelMessage{ID: uuid.NewString(), ChannelID: ch, SenderID: "operator", Content: "continue"},
		ChannelTypeGroup, members, len(members), false, nil, stale)

	assert.False(t, closed, "round 2 crossed the OLD bound only — the raised config extends the discussion")
	assert.False(t, staleVerdict, "…and withholds nothing: the message is live traffic of the extended round")
	assert.Empty(t, disp.synthesisTurns(), "no synthesis close is armed off the stale round bound")
	assert.Zero(t, closedCount(t, reader, structuralTrigger))
	_, _, tracked := router.openInteractionEscalationState(ch)
	assert.True(t, tracked, "the interaction stays open under the raised bound")

	// The crossed tally survived the deferral: the RAISED bound still
	// terminates deterministically at its own round count.
	tick(t, router, ch) // round 3
	tick(t, router, ch) // round 4
	tick(t, router, ch) // round 5 == the raised bound
	require.Len(t, disp.synthesisTurns(), 1,
		"the raised bound arms the (chaired) close at its own round count — the tally was deferred, not reset")
}

// failOneLiveDispatcher records everything and fails the LIVE dispatches (and
// only those) to one member — the per-recipient timeout/dial-error shape the
// fanout treats as warn-only, fire-and-forget.
type failOneLiveDispatcher struct {
	dispatchRecorder
	failFor string
}

func (d *failOneLiveDispatcher) Dispatch(ctx context.Context, env DispatchEnvelope, msg ChannelMessage) error {
	_ = d.dispatchRecorder.Dispatch(ctx, env, msg)
	if env.Recipient.ParticipantID == d.failFor && !env.InteractionCloseNotification {
		return errors.New("per-recipient dial timeout")
	}
	return nil
}

// TestBoundedClose_RedeliveryResolvedPerRecipient — the floor-path bounded
// close stamps `close_notification_redelivery=true` because the round already
// delivered the bounding stimulus live; but delivery is per-recipient, and a
// member the round FAILED to reach then skipped the notification ingest too —
// its record closed permanently missing the closing turn (pre-4b-ii the
// notification re-ingest doubled as its delivery repair). The round's failures
// now downgrade exactly those members to sole delivery.
func TestBoundedClose_RedeliveryResolvedPerRecipient(t *testing.T) {
	disp := &failOneLiveDispatcher{failFor: "iron-fox"}
	store := newTestStore(t, SQLiteOptions{})
	router := NewChannelRouter(store, disp, zap.NewNop(), nil)
	// Chairless on purpose: the immediate 4b-i close is the redelivery-marked
	// path (a chaired bound would arm and close on the reply instead).
	ch := mustCreateGroupWithPolicies(t, store, "brainstorm",
		map[string]RespondPolicy{
			"operator":  RespondNever,
			"ember-owl": RespondAlways,
			"iron-fox":  RespondAlways,
		}, "operator", "ember-owl", "iron-fox")
	router.SetFloorControl(ch, true, time.Millisecond)
	router.SetAutonomous(ch, AutonomousConfig{Enabled: true, MaxRounds: 1, Convener: "ember-owl"})

	// One publish = the bounding floor round: ember-owl's dispatch lands,
	// iron-fox's fails, and the tail closes with redelivery.
	tick(t, router, ch)
	router.WaitForPendingFanout()

	notifications := disp.closeNotifications()
	require.NotEmpty(t, notifications, "the bounded close fans its notification")
	seen := map[string]bool{}
	for _, c := range notifications {
		seen[c.env.Recipient.ParticipantID] = true
		switch c.env.Recipient.ParticipantID {
		case "iron-fox":
			assert.False(t, c.env.InteractionCloseRedelivery,
				"a member the live round failed to reach gets a SOLE delivery — the ingest-skip must not drop its closing turn")
		default:
			assert.True(t, c.env.InteractionCloseRedelivery,
				"a member the live round reached keeps the redelivery skip — no duplicate final turn")
		}
	}
	assert.True(t, seen["ember-owl"] && seen["iron-fox"],
		"both discussion members are notified either way")
}

// missShapesDispatcher fails the LIVE dispatches to two members with the two
// delivery-miss error shapes [GRPCMessageDispatcher.Dispatch] used to
// nil-swallow (PR #718 follow-up review): the wrapped
// [registry.ErrAgentNotFound] of an unregistered target, and the
// refused-ack error of a TaskAck with success=false (the agent servicer's
// queue-full discard / validation failure). Close notifications pass so the
// per-recipient marker can be asserted.
type missShapesDispatcher struct {
	dispatchRecorder
	notRegistered string
	ackRefused    string
}

func (d *missShapesDispatcher) Dispatch(ctx context.Context, env DispatchEnvelope, msg ChannelMessage) error {
	_ = d.dispatchRecorder.Dispatch(ctx, env, msg)
	if env.InteractionCloseNotification {
		return nil
	}
	switch env.Recipient.ParticipantID {
	case d.notRegistered:
		return fmt.Errorf("dispatch target %s not registered: %w", env.Recipient.ParticipantID, registry.ErrAgentNotFound)
	case d.ackRefused:
		return fmt.Errorf("ReceiveChannelMessage to %s: receiver refused delivery: event queue full; message discarded", env.Recipient.ParticipantID)
	}
	return nil
}

// TestBoundedClose_DeliveryMissShapesDowngradeToSoleDelivery — the redelivery
// ledger must see BOTH miss shapes the dispatcher used to report as nil: an
// unregistered recipient and a recipient whose receiver refused the ack. Both
// members land in the round's undelivered set and their close notifications
// carry redelivery=false (sole delivery — the ingest skip must not drop a
// closing turn that never arrived), while a member the round genuinely
// reached keeps the redelivery skip.
func TestBoundedClose_DeliveryMissShapesDowngradeToSoleDelivery(t *testing.T) {
	disp := &missShapesDispatcher{notRegistered: "iron-fox", ackRefused: "pine-marten"}
	store := newTestStore(t, SQLiteOptions{})
	router := NewChannelRouter(store, disp, zap.NewNop(), nil)
	// Chairless on purpose, like the per-recipient sibling above: the
	// immediate 4b-i close is the redelivery-marked path.
	ch := mustCreateGroupWithPolicies(t, store, "brainstorm",
		map[string]RespondPolicy{
			"operator":    RespondNever,
			"ember-owl":   RespondAlways,
			"iron-fox":    RespondAlways,
			"pine-marten": RespondAlways,
		}, "operator", "ember-owl", "iron-fox", "pine-marten")
	router.SetFloorControl(ch, true, time.Millisecond)
	router.SetAutonomous(ch, AutonomousConfig{Enabled: true, MaxRounds: 1, Convener: "ember-owl"})

	// One publish = the bounding floor round: ember-owl's dispatch lands,
	// iron-fox's misses as not-registered, pine-marten's as a refused ack.
	tick(t, router, ch)
	router.WaitForPendingFanout()

	notifications := disp.closeNotifications()
	require.NotEmpty(t, notifications, "the bounded close fans its notification")
	seen := map[string]bool{}
	for _, c := range notifications {
		seen[c.env.Recipient.ParticipantID] = true
		switch c.env.Recipient.ParticipantID {
		case "iron-fox":
			assert.False(t, c.env.InteractionCloseRedelivery,
				"an unregistered member's miss must reach the undelivered ledger — sole delivery")
		case "pine-marten":
			assert.False(t, c.env.InteractionCloseRedelivery,
				"a refused-ack member's miss must reach the undelivered ledger — sole delivery")
		default:
			assert.True(t, c.env.InteractionCloseRedelivery,
				"a member the live round reached keeps the redelivery skip")
		}
	}
	assert.True(t, seen["ember-owl"] && seen["iron-fox"] && seen["pine-marten"],
		"every discussion member is notified either way")
}
