package channels

// bounded_close_test.go — RFC 0052 §D deterministic bounded close, PR 4b-i
// (docs/rfcs/0052-pr-plan.md). TDD-first: this matrix was written red against the
// planned API and pins the orchestrator-side terminator — the floor-round tally
// enforcing `autonomous.max_rounds` (which had NO enforcement before), the wallet
// SOFT-budget close, the `interaction_closed{trigger=structural|cost}` contract,
// the artifact-bearing teardown (the id is retired so the channel is
// re-convenable), and — the load-bearing safety invariant — that the whole
// mechanism is scoped to `autonomous.enabled` so human channels are byte-for-byte
// unchanged.

import (
	"context"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
	"go.uber.org/zap"
)

// fakeSpender is a stand-in for the wallet's per-interaction running total
// ([interactionSpender]); it reports a fixed spend for every interaction so the
// soft-budget trigger can be exercised deterministically without a real wallet.
type fakeSpender struct{ v int64 }

func (f fakeSpender) InteractionSpend(string) int64 { return f.v }

// boundedCloseHarness builds a floor-controlled autonomous group whose two
// personas never reply (the recorder swallows dispatches) and a 1ms turn timeout
// so each stalled round completes immediately. Each publish from the RespondNever
// operator seat is therefore exactly one floor round — one bounded-close tally
// tick. `enabled` toggles the autonomous arm so the same stage drives both the
// autonomous and the human-channel-regression legs.
func boundedCloseHarness(t *testing.T, enabled bool, maxRounds int) (*ChannelRouter, *envelopeRecorder, string, *sdkmetric.ManualReader) {
	t.Helper()
	reader := sdkmetric.NewManualReader()
	mp := sdkmetric.NewMeterProvider(sdkmetric.WithReader(reader))
	t.Cleanup(func() { _ = mp.Shutdown(context.Background()) })
	ctr, err := mp.Meter("test").Int64Counter("channel.conversation.interaction_closed")
	require.NoError(t, err)
	store := newTestStore(t, SQLiteOptions{})
	disp := &envelopeRecorder{}
	router := NewChannelRouter(store, disp, zap.NewNop(), &RouterMetrics{InteractionClosed: ctr})
	ch := mustCreateGroupWithPolicies(t, store, "brainstorm",
		map[string]RespondPolicy{
			"operator":  RespondNever, // the stimulus author (no seat in the discussion)
			"ember-owl": RespondAlways,
			"iron-fox":  RespondAlways,
		}, "operator", "ember-owl", "iron-fox")
	router.SetFloorControl(ch, true, time.Millisecond)
	router.SetAutonomous(ch, AutonomousConfig{Enabled: enabled, MaxRounds: maxRounds, Convener: "ember-owl"})
	return router, disp, ch, reader
}

// tick publishes one open-floor stimulus from the operator seat; with the
// recorder never replying it is one stalled floor round.
func tick(t *testing.T, router *ChannelRouter, ch string) {
	t.Helper()
	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: ch, SenderID: "operator", Content: "continue",
	}, ""))
}

func closedCount(t *testing.T, reader *sdkmetric.ManualReader, trigger string) int64 {
	t.Helper()
	var rm metricdata.ResourceMetrics
	require.NoError(t, reader.Collect(context.Background(), &rm))
	return interactionClosedCount(t, rm, "group", trigger)
}

// TestBoundedClose_MaxRoundsClosesInteraction — the structural terminator: an
// autonomous interaction that runs `max_rounds` floor rounds closes on the last,
// emitting interaction_closed{trigger=structural}, and NOT before.
func TestBoundedClose_MaxRoundsClosesInteraction(t *testing.T) {
	router, _, ch, reader := boundedCloseHarness(t, true, 3)

	tick(t, router, ch)
	tick(t, router, ch)
	assert.Zero(t, closedCount(t, reader, structuralTrigger), "no close before the round bound")

	tick(t, router, ch) // the 3rd round hits max_rounds

	assert.Equal(t, int64(1), closedCount(t, reader, structuralTrigger),
		"the max_rounds round closes the interaction")
	assert.Zero(t, closedCount(t, reader, costTrigger), "the budget terminator did not fire")

	// The id is retired (IP8) so the channel is re-convenable — the next publish
	// mints fresh rather than stamping the closed id.
	_, _, tracked := router.openInteractionEscalationState(ch)
	assert.False(t, tracked, "the bounded close retires the interaction id")
}

// TestBoundedClose_SoftBudgetClosesInteraction — the cost terminator: when the
// wallet running spend crosses the SOFT budget (cap minus the PR 4a synthesis
// reserve), the interaction closes with trigger=cost BEFORE max_rounds.
func TestBoundedClose_SoftBudgetClosesInteraction(t *testing.T) {
	router, _, ch, reader := boundedCloseHarness(t, true, 100) // round bound out of reach
	router.SetInteractionBudgetTokens(ch, 100_000)
	router.SetInteractionSpender(fakeSpender{v: 100_000}) // >= any soft threshold

	tick(t, router, ch) // first round: spend already over the soft budget

	assert.Equal(t, int64(1), closedCount(t, reader, costTrigger),
		"crossing the soft budget closes the interaction")
	assert.Zero(t, closedCount(t, reader, structuralTrigger), "the round terminator did not fire")
}

// TestBoundedClose_HumanChannelUntouched — the OQ #2 scope invariant: a channel
// with autonomous disabled never bounded-closes, no matter how many rounds run.
func TestBoundedClose_HumanChannelUntouched(t *testing.T) {
	router, _, ch, reader := boundedCloseHarness(t, false, 3) // autonomous OFF
	router.SetInteractionBudgetTokens(ch, 100_000)
	router.SetInteractionSpender(fakeSpender{v: 100_000})

	for i := 0; i < 5; i++ { // well past what would be the round bound
		tick(t, router, ch)
	}

	assert.Zero(t, closedCount(t, reader, structuralTrigger), "human channels never structural-close")
	assert.Zero(t, closedCount(t, reader, costTrigger), "human channels never cost-close")
	_, _, tracked := router.openInteractionEscalationState(ch)
	assert.True(t, tracked, "the human interaction stays open")
}

// TestBoundedClose_NotBeforeBound — a sub-bound autonomous interaction (rounds
// below max_rounds, no budget pressure) does not close.
func TestBoundedClose_NotBeforeBound(t *testing.T) {
	router, _, ch, reader := boundedCloseHarness(t, true, 5)

	tick(t, router, ch)
	tick(t, router, ch)

	assert.Zero(t, closedCount(t, reader, structuralTrigger))
	assert.Zero(t, closedCount(t, reader, costTrigger))
	_, _, tracked := router.openInteractionEscalationState(ch)
	assert.True(t, tracked, "a sub-bound interaction stays open")
}

// TestBoundedClose_FreshInteractionFreshTally — the tally rides the resolver
// entry and dies with the interaction: after a bounded close, a fresh interaction
// starts a fresh round count, so the channel can be re-convened and run again.
func TestBoundedClose_FreshInteractionFreshTally(t *testing.T) {
	router, _, ch, reader := boundedCloseHarness(t, true, 2)
	now := time.Date(2026, 7, 1, 12, 0, 0, 0, time.UTC)
	router.interactionNow = func() time.Time { return now }

	tick(t, router, ch)
	tick(t, router, ch) // round 2 == max_rounds → close
	require.Equal(t, int64(1), closedCount(t, reader, structuralTrigger))

	// A fresh interaction (past the idle window) runs its own two rounds and
	// closes again — the tally did not carry over.
	now = now.Add(601 * time.Second)
	tick(t, router, ch)
	tick(t, router, ch)

	assert.Equal(t, int64(2), closedCount(t, reader, structuralTrigger),
		"a re-convened interaction bounds independently")
}

// TestBoundedClose_ClosesOnTheBoundingRound — a max_rounds of 1 closes on the
// very first floor round: the tally is compared with `>=`, so the bounding round
// itself terminates (exactly once) and retires the id.
func TestBoundedClose_ClosesOnTheBoundingRound(t *testing.T) {
	router, _, ch, reader := boundedCloseHarness(t, true, 1) // the first round is the bound

	tick(t, router, ch) // round 1 == max_rounds → close

	assert.Equal(t, int64(1), closedCount(t, reader, structuralTrigger),
		"the bounding round closes the interaction exactly once")
	_, _, tracked := router.openInteractionEscalationState(ch)
	assert.False(t, tracked)
}

// TestBoundedClose_ConcurrentPathClosesInteraction pins the OTHER fanout tail
// (fanout.go, the concurrent path) that the floor-controlled harness never
// exercises: an autonomous channel with floor control OFF routes every publish
// through dispatchConcurrent, and the bounded close must still fire there. Here
// the tally counts messages (no floor round), so max_rounds=2 closes on the 2nd.
func TestBoundedClose_ConcurrentPathClosesInteraction(t *testing.T) {
	reader := sdkmetric.NewManualReader()
	mp := sdkmetric.NewMeterProvider(sdkmetric.WithReader(reader))
	t.Cleanup(func() { _ = mp.Shutdown(context.Background()) })
	ctr, err := mp.Meter("test").Int64Counter("channel.conversation.interaction_closed")
	require.NoError(t, err)
	store := newTestStore(t, SQLiteOptions{})
	router := NewChannelRouter(store, &envelopeRecorder{}, zap.NewNop(), &RouterMetrics{InteractionClosed: ctr})
	// Floor control deliberately left OFF → the fanout falls through to the
	// concurrent path and maybeBoundedClose runs at that tail instead.
	ch := mustCreateGroupWithPolicies(t, store, "brainstorm",
		map[string]RespondPolicy{
			"operator":  RespondNever,
			"ember-owl": RespondAlways,
		}, "operator", "ember-owl")
	router.SetAutonomous(ch, AutonomousConfig{Enabled: true, MaxRounds: 2, Convener: "ember-owl"})

	tick(t, router, ch)
	assert.Zero(t, closedCount(t, reader, structuralTrigger), "no close before the round bound")

	tick(t, router, ch) // the 2nd message on the concurrent path hits max_rounds

	assert.Equal(t, int64(1), closedCount(t, reader, structuralTrigger),
		"the concurrent-path tail closes the interaction at max_rounds")
	_, _, tracked := router.openInteractionEscalationState(ch)
	assert.False(t, tracked, "the concurrent-path bounded close retires the id too")
}

// TestBoundedClose_NotifiesTriggeringSender is the regression for the deep-review
// finding: the bounded close reuses notifyInteractionClose, which excludes
// msg.SenderID — correct for the end-vote close (the voter self-closed) but WRONG
// here, where the sender is only the round-triggering participant. Every
// dispatch-served member, INCLUDING the sender, must get the close notification so
// it produces its RFC 0020 summary rather than idling out mislabeled.
func TestBoundedClose_NotifiesTriggeringSender(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	disp := &envelopeRecorder{}
	router := NewChannelRouter(store, disp, zap.NewNop(), nil)
	ch := mustCreateGroupWithPolicies(t, store, "brainstorm",
		map[string]RespondPolicy{
			"chair-x":   RespondAlways, // the triggering sender — a real participant
			"ember-owl": RespondAlways,
			"iron-fox":  RespondAlways,
		}, "chair-x", "ember-owl", "iron-fox")
	router.SetFloorControl(ch, true, time.Millisecond)
	router.SetAutonomous(ch, AutonomousConfig{Enabled: true, MaxRounds: 1, Convener: "ember-owl"})

	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: ch, SenderID: "chair-x", Content: "let's wrap",
	}, ""))
	router.WaitForPendingFanout()

	notified := map[string]bool{}
	for _, env := range disp.snapshot() {
		if env.InteractionCloseNotification {
			notified[env.Recipient.ParticipantID] = true
		}
	}
	assert.True(t, notified["chair-x"], "the round-triggering sender must receive the bounded close notification")
	assert.True(t, notified["ember-owl"], "other members receive it too")
	assert.True(t, notified["iron-fox"], "other members receive it too")
}

// TestBoundedClose_SuppressesEscalationOnBoundingRound pins the deep-review
// ordering fix: when the bounding round is also a stall, the bounded close runs
// BEFORE the chair-stall escalation and retires the id, so the escalation tail
// no-ops. Otherwise the forced chair turn is dispatched onto a closing
// interaction and its reply would mint a FRESH interaction, reopening the
// discussion the close just terminated.
func TestBoundedClose_SuppressesEscalationOnBoundingRound(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	disp := &envelopeRecorder{}
	router := NewChannelRouter(store, disp, zap.NewNop(), nil)
	ch := mustCreateGroupWithPolicies(t, store, "planning",
		map[string]RespondPolicy{
			"operator":     RespondNever, // stimulus author
			"ember-owl":    RespondAlways,
			"nova-sparrow": RespondAlways, // the escalation chair
		}, "operator", "ember-owl", "nova-sparrow")
	router.SetFloorControl(ch, true, time.Millisecond)
	router.SetEscalationChair(ch, "nova-sparrow")
	router.SetAutonomous(ch, AutonomousConfig{Enabled: true, MaxRounds: 1, Convener: "ember-owl"})

	// Personas never reply (recorder swallows) → the round stalls, and it is the
	// bounding round (max_rounds=1). The close must win the tail.
	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: ch, SenderID: "operator", Content: "thoughts?",
	}, ""))
	router.WaitForPendingFanout()

	_, _, tracked := router.openInteractionEscalationState(ch)
	assert.False(t, tracked, "the bounding round closes the interaction")
	for _, env := range disp.snapshot() {
		assert.False(t, env.ChairEscalation,
			"no forced chair turn on the bounding round — the close retired the id first")
	}
}
