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
	"go.opentelemetry.io/otel/metric"
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
	return autonomousCloseHarness(t, enabled, maxRounds, zap.NewNop(), nil)
}

// autonomousCloseHarness is the stage [boundedCloseHarness] and
// bounded_close_reserve_test.go's `reserveHarness` share — ONE definition of the
// three-seat room every close test's arithmetic is written against (one
// RespondNever operator + two personas, so `channelSize` is 3), because the two
// files hard-code expectations derived from that roster and a silently
// divergent copy would invalidate one of them.
//
// `instrument` registers any additional counters the caller needs on the SAME
// meter provider, and runs before [NewChannelRouter] because [RouterMetrics] is
// read once at construction. `logger` is the caller's, so a test can assert on
// the log lines the close path emits (the clamp Warn) instead of discarding
// them; pass `zap.NewNop()` when it does not care.
func autonomousCloseHarness(
	t *testing.T,
	enabled bool,
	maxRounds int,
	logger *zap.Logger,
	instrument func(metric.Meter, *RouterMetrics),
) (*ChannelRouter, *envelopeRecorder, string, *sdkmetric.ManualReader) {
	t.Helper()
	reader := sdkmetric.NewManualReader()
	mp := sdkmetric.NewMeterProvider(sdkmetric.WithReader(reader))
	t.Cleanup(func() { _ = mp.Shutdown(context.Background()) })
	meter := mp.Meter("test")
	ctr, err := meter.Int64Counter("channel.conversation.interaction_closed")
	require.NoError(t, err)
	rm := &RouterMetrics{InteractionClosed: ctr}
	if instrument != nil {
		instrument(meter, rm)
	}
	store := newTestStore(t, SQLiteOptions{})
	disp := &envelopeRecorder{}
	router := NewChannelRouter(store, disp, logger, rm)
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

// concurrentCloseHarness is boundedCloseHarness's CONCURRENT-path sibling
// (PR #716 review — one stage for the six tests that hand-rolled it): the same
// autonomous group with floor control OFF and a single responder, so every
// publish falls through to dispatchConcurrent and the bounded close runs at
// that tail; here the tally counts messages, not floor rounds. Autonomous is
// always ON — the human-channel regression rides the floor harness.
func concurrentCloseHarness(t *testing.T, maxRounds int) (*ChannelRouter, *envelopeRecorder, string, *sdkmetric.ManualReader) {
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
		}, "operator", "ember-owl")
	router.SetAutonomous(ch, AutonomousConfig{Enabled: true, MaxRounds: maxRounds, Convener: "ember-owl"})
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
	router, _, ch, reader := concurrentCloseHarness(t, 2)

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

// TestBoundedClose_ConcurrentPathDoesNotDispatchBoundingStimulus is the
// deep-review regression for the concurrent-path REOPEN: on that path a
// dispatched reply re-enters Publish (no floor-speaker suppression), so
// dispatching the bounding stimulus and THEN closing would let the reply mint a
// fresh interaction and reopen the just-closed discussion — an endless
// convene→bound→reopen loop on any single-responder autonomous round. The fix
// runs the close BEFORE the dispatch and skips the live dispatch on close: the
// bounding message goes out only as the close notification (which the recipient
// ingests as its final turn and does NOT reply to), never as a fresh stimulus.
//
// Discriminating: with dispatch-then-close the responder would see TWO ordinary
// stimuli (rounds 1 and 2); the fix leaves exactly one (round 1) plus one close
// notification.
func TestBoundedClose_ConcurrentPathDoesNotDispatchBoundingStimulus(t *testing.T) {
	router, disp, ch, _ := concurrentCloseHarness(t, 2)

	tick(t, router, ch) // round 1: dispatched normally
	tick(t, router, ch) // round 2 == max_rounds: closes; the stimulus must NOT be dispatched
	router.WaitForPendingFanout()

	var ordinary, closeNotifications int
	for _, env := range disp.snapshot() {
		if env.Recipient.ParticipantID != "ember-owl" {
			continue
		}
		if env.InteractionCloseNotification {
			closeNotifications++
		} else {
			ordinary++
		}
	}
	assert.Equal(t, 1, ordinary,
		"only round 1 is dispatched live; the bounding stimulus is withheld so its reply cannot reopen the interaction")
	assert.Equal(t, 1, closeNotifications,
		"the bounding message reaches the responder as the close notification instead (ingested as the final turn, not replied to)")
}

// TestBoundedClose_ConcurrentPathTinyBoundStillDeliversOpener — the PR #716
// review §D artifact pin: `max_rounds = 1` is a legal config (validation
// rejects only negatives; zero fills the default), and on the concurrent path
// the close runs BEFORE the dispatch — so pre-fix the bounding round WAS the
// opening turn: the opener was withheld, no member ever opened a scope, the
// close notification no-opped agent-side (close_notification.py), and the
// channel closed having delivered nothing and produced no artifact — a
// silently useless convene. The round-1 guard defers the close past the
// interaction's FIRST live dispatch: the opener goes out live and the next
// tail closes — one live exchange, artifact guaranteed, the same meaning
// `max_rounds = 1` has on the floor path (whose bounding round runs before it
// counts — TestBoundedClose_ClosesOnTheBoundingRound).
func TestBoundedClose_ConcurrentPathTinyBoundStillDeliversOpener(t *testing.T) {
	router, disp, ch, _ := concurrentCloseHarness(t, 1)

	tick(t, router, ch) // round 1 == max_rounds — but it is the opening turn
	router.WaitForPendingFanout()

	var ordinary int
	for _, env := range disp.snapshot() {
		if env.Recipient.ParticipantID == "ember-owl" && !env.InteractionCloseNotification {
			ordinary++
		}
	}
	assert.Equal(t, 1, ordinary,
		"the opening turn is dispatched live — withholding it would close an empty record")
	_, _, tracked := router.openInteractionEscalationState(ch)
	require.True(t, tracked, "the interaction survives its opening round")

	tick(t, router, ch) // round 2: past the deferred bound → close, dispatch withheld
	router.WaitForPendingFanout()

	_, _, tracked = router.openInteractionEscalationState(ch)
	assert.False(t, tracked, "the next tail closes the interaction")
	var closeNotifications int
	for _, env := range disp.snapshot() {
		if env.Recipient.ParticipantID == "ember-owl" && env.InteractionCloseNotification {
			closeNotifications++
		}
	}
	assert.Equal(t, 1, closeNotifications,
		"the close notification reaches the member whose scope the live opener opened — the §D artifact")
}

// TestBoundedClose_ConcurrentPathClearsActivityMarks is the review round 5
// presence regression: the concurrent-path bounding round marks its responders
// "thinking" at the fanout head (RFC 0048 Tier 1) and then WITHHOLDS the
// dispatch — so no reply can ever re-enter publishCommit to clear the marks,
// the same "no reply can ever clear it" condition the escalation error
// branches unmark for (chair_escalation.go). Without the close-branch clear,
// the console strands every responder "thinking" for the full activityTTL
// (90s) on a discussion that just terminated.
func TestBoundedClose_ConcurrentPathClearsActivityMarks(t *testing.T) {
	router, _, ch, _ := concurrentCloseHarness(t, 2)

	tick(t, router, ch) // round 1: dispatched live; the mark stands until a reply or the TTL
	tick(t, router, ch) // round 2 == max_rounds: close fires, dispatch withheld
	router.WaitForPendingFanout()

	assert.Empty(t, router.ChannelActivity(ch),
		"the withheld bounding dispatch draws no reply — the close must clear the responders' thinking marks, not strand them until the TTL")
}

// TestBoundedClose_ConcurrentPathStaleStimulusNotDispatched — the PR #716
// review racing-sibling pin: a concurrent-path fanout whose stimulus committed
// BEFORE a sibling's bounded close but whose tail runs AFTER it must not
// dispatch — the stimulus outlived its interaction, and fanning it live would
// draw LLM replies into the terminated discussion (replies the no-reopen latch
// then absorbs, with the spend already spent). maybeBoundedClose's `stale`
// return distinguishes exactly this from a live sub-bound round; pre-fix both
// fell into the same `false` and the stale stimulus fanned. The tail is driven
// directly (fanout with the retired id stamped) because everything downstream
// of the commit is fanout's — the sibling interleaving has no deterministic
// seam through Publish.
func TestBoundedClose_ConcurrentPathStaleStimulusNotDispatched(t *testing.T) {
	router, disp, ch, reader := concurrentCloseHarness(t, 2)

	tick(t, router, ch) // round 1: opens the interaction, dispatched live
	staleID, _, tracked := router.openInteractionEscalationState(ch)
	require.True(t, tracked)
	tick(t, router, ch) // round 2 == max_rounds → the "sibling" close retires the id
	router.WaitForPendingFanout()
	_, _, tracked = router.openInteractionEscalationState(ch)
	require.False(t, tracked, "the sibling bounded close retired the id")
	require.Equal(t, int64(1), closedCount(t, reader, structuralTrigger))
	before := liveDispatches(disp)

	// The racing sibling's tail: its stimulus committed under the now-retired
	// id (resolver-stamped pre-close) and its detached fanout runs only now.
	router.fanout(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: ch, SenderID: "operator", Content: "raced the close",
		Metadata: map[string]any{interactionIDMetadataKey: staleID},
	}, ChannelTypeGroup, "")

	assert.Equal(t, before, liveDispatches(disp),
		"a stale stimulus is withheld — dispatching it would draw replies into a closed discussion")
	assert.Equal(t, int64(1), closedCount(t, reader, structuralTrigger), "and the stale tail does not double-close")
	assert.Empty(t, router.ChannelActivity(ch),
		"the withheld dispatch clears its head marks — no reply will ever re-enter to clear them")
}
