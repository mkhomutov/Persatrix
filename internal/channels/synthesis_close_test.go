package channels

// synthesis_close_test.go — RFC 0052 §D goal-directed chair synthesis turn,
// PR 4b-ii (docs/rfcs/0052-pr-plan.md). TDD-first: this matrix was written red
// against the planned API and pins the CLOSE-ON-REPLY ordering — a bound-crossing
// round on a chaired autonomous channel no longer closes immediately; it
// dispatches the synthesis forced turn to the escalation chair, withholds all
// further discussion traffic, and the chair's claimed reply IS the closing
// artifact the close-notification fan carries to every member (redelivery=false —
// sole delivery). The chair proposes, the ORCHESTRATOR disposes: CE4 intact. A
// missing/drifted chair, a failed dispatch, or a reply that never arrives (the
// timeout net) all degrade to the PR 4b-i immediate artifact-bearing close, so
// termination stays deterministic — an LLM reply is never load-bearing for the
// bound itself.

import (
	"context"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
	"go.uber.org/zap"
)

// dispatchRecorder is the envelopeRecorder's (env, msg) sibling: the synthesis
// tests must assert the dispatched MESSAGE too (the directive's goal content,
// the interaction-id claim it carries, the closing artifact the notification
// fan re-delivers), which the envelope alone does not hold.
type dispatchRecorder struct {
	mu    sync.Mutex
	calls []recordedDispatch
}

type recordedDispatch struct {
	env DispatchEnvelope
	msg ChannelMessage
}

func (d *dispatchRecorder) Dispatch(_ context.Context, env DispatchEnvelope, msg ChannelMessage) error {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.calls = append(d.calls, recordedDispatch{env: env, msg: msg})
	return nil
}

func (d *dispatchRecorder) snapshot() []recordedDispatch {
	d.mu.Lock()
	defer d.mu.Unlock()
	out := make([]recordedDispatch, len(d.calls))
	copy(out, d.calls)
	return out
}

func (d *dispatchRecorder) synthesisTurns() []recordedDispatch {
	var out []recordedDispatch
	for _, c := range d.snapshot() {
		if c.env.SynthesisTurn {
			out = append(out, c)
		}
	}
	return out
}

func (d *dispatchRecorder) closeNotifications() []recordedDispatch {
	var out []recordedDispatch
	for _, c := range d.snapshot() {
		if c.env.InteractionCloseNotification {
			out = append(out, c)
		}
	}
	return out
}

// synthesisCloseHarness is boundedCloseHarness with the PR 4a mandatory chair
// actually configured (the harness the 4b-i tests deliberately left chairless,
// which is now the immediate-close FALLBACK path) plus an operator goal for the
// directive, a (msg-recording) dispatcher, and the synthesis_turn counter.
func synthesisCloseHarness(t *testing.T, maxRounds int) (*ChannelRouter, *dispatchRecorder, string, *sdkmetric.ManualReader) {
	t.Helper()
	reader := sdkmetric.NewManualReader()
	mp := sdkmetric.NewMeterProvider(sdkmetric.WithReader(reader))
	t.Cleanup(func() { _ = mp.Shutdown(context.Background()) })
	closed, err := mp.Meter("test").Int64Counter("channel.conversation.interaction_closed")
	require.NoError(t, err)
	synth, err := mp.Meter("test").Int64Counter("channel.conversation.synthesis_turn")
	require.NoError(t, err)
	store := newTestStore(t, SQLiteOptions{})
	disp := &dispatchRecorder{}
	router := NewChannelRouter(store, disp, zap.NewNop(), &RouterMetrics{
		InteractionClosed: closed,
		SynthesisTurn:     synth,
	})
	ch := mustCreateGroupWithPolicies(t, store, "brainstorm",
		map[string]RespondPolicy{
			"operator":  RespondNever, // the stimulus author (no seat in the discussion)
			"ember-owl": RespondAlways,
			"iron-fox":  RespondAlways,
		}, "operator", "ember-owl", "iron-fox")
	router.SetFloorControl(ch, true, time.Millisecond)
	router.SetAutonomous(ch, AutonomousConfig{
		Enabled: true, MaxRounds: maxRounds, Convener: "ember-owl",
		Topic: "Adopt a monorepo?", Goal: "A synthesized recommendation.",
	})
	router.SetEscalationChair(ch, "iron-fox")
	return router, disp, ch, reader
}

// chairReply publishes the chair's synthesis reply: a same-channel publish
// echoing the interaction id it was dispatched under as the wire claim — the
// PR 4b-i origin-pair echo every agent reply now carries.
func chairReply(t *testing.T, router *ChannelRouter, ch, chairID, interactionID, content string) {
	t.Helper()
	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: ch, SenderID: chairID, Content: content,
		Metadata: map[string]any{"interaction_id": interactionID},
	}, ""))
}

func synthesisTurnCount(t *testing.T, reader *sdkmetric.ManualReader, outcome string) int64 {
	t.Helper()
	var rm metricdata.ResourceMetrics
	require.NoError(t, reader.Collect(context.Background(), &rm))
	for _, sm := range rm.ScopeMetrics {
		for _, m := range sm.Metrics {
			if m.Name != "channel.conversation.synthesis_turn" {
				continue
			}
			sum, ok := m.Data.(metricdata.Sum[int64])
			require.Truef(t, ok, "synthesis_turn: expected Sum[int64], got %T", m.Data)
			for _, dp := range sum.DataPoints {
				o, _ := dp.Attributes.Value("outcome")
				if o.AsString() == outcome {
					return dp.Value
				}
			}
		}
	}
	return 0
}

// TestSynthesisClose_BoundDispatchesChairTurnNotClose — the ordering §D pins:
// on a chaired channel the bound dispatches the synthesis turn and does NOT
// close yet; the directive is a directed, marked, synthetically-sent dispatch
// to the chair carrying the operator goal and the closing interaction's id
// (the claim the reply must echo back).
func TestSynthesisClose_BoundDispatchesChairTurnNotClose(t *testing.T) {
	router, disp, ch, reader := synthesisCloseHarness(t, 2)

	openID, _, tracked := func() (string, bool, bool) {
		tick(t, router, ch)
		id, esc, tr := router.openInteractionEscalationState(ch)
		return id, esc, tr
	}()
	require.True(t, tracked)
	tick(t, router, ch) // the bounding round

	assert.Zero(t, closedCount(t, reader, structuralTrigger),
		"a chaired bound does not close immediately — the synthesis reply (or the timeout) closes")
	turns := disp.synthesisTurns()
	require.Len(t, turns, 1, "exactly one synthesis forced turn")
	turn := turns[0]
	assert.Equal(t, "iron-fox", turn.env.Recipient.ParticipantID, "dispatched to the escalation chair")
	assert.Equal(t, SynthesisDispatchSenderID, turn.msg.SenderID,
		"synthetic sender — never a legal participant id, so the gate's self-sender defence cannot misfire")
	assert.Contains(t, turn.msg.Content, "A synthesized recommendation.",
		"the directive carries the operator goal")
	assert.Equal(t, openID, readInteractionID(turn.msg.Metadata),
		"the directive claims the closing interaction — the id the reply echoes back")
	assert.Equal(t, int64(1), synthesisTurnCount(t, reader, "dispatched"))
}

// TestSynthesisClose_ChairReplyIsTheClosingArtifact — close-on-reply: the
// chair's claimed reply closes the interaction, and the close-notification fan
// carries THE REPLY (sole delivery — redelivery=false) with the truthful
// structural trigger to every member, the round-triggering sender included.
// The reply itself never re-fans as a stimulus — no reopened round.
func TestSynthesisClose_ChairReplyIsTheClosingArtifact(t *testing.T) {
	router, disp, ch, reader := synthesisCloseHarness(t, 2)

	tick(t, router, ch)
	openID, _, _ := router.openInteractionEscalationState(ch)
	tick(t, router, ch) // bound → synthesis turn dispatched, close pending
	before := len(disp.snapshot())

	chairReply(t, router, ch, "iron-fox", openID, "Synthesis: adopt the monorepo; revisit tooling in Q3.")
	router.WaitForPendingFanout()

	assert.Equal(t, int64(1), closedCount(t, reader, structuralTrigger),
		"the synthesis reply closes the interaction")
	assert.Equal(t, int64(1), synthesisTurnCount(t, reader, "closed_on_reply"))
	_, _, tracked := router.openInteractionEscalationState(ch)
	assert.False(t, tracked, "the id is retired — the channel is re-convenable")

	notified := map[string]bool{}
	for _, c := range disp.closeNotifications() {
		notified[c.env.Recipient.ParticipantID] = true
		assert.Contains(t, c.msg.Content, "Synthesis: adopt the monorepo",
			"the close notification carries the synthesis — the §D artifact every member ingests")
		assert.Equal(t, structuralTrigger, c.env.InteractionCloseTrigger,
			"the truthful bounded-close cause rides the notification (the OQ #6 metering key)")
		assert.False(t, c.env.InteractionCloseRedelivery,
			"the synthesis was intercepted at the fanout head, never fanned — sole delivery")
	}
	assert.True(t, notified["ember-owl"] && notified["iron-fox"] && notified["operator"] == false,
		"every dispatch-served member is notified (RespondNever operator excluded by contract)")

	for _, c := range disp.snapshot()[before:] {
		if c.env.InteractionCloseNotification || c.env.SynthesisTurn {
			continue
		}
		assert.NotContains(t, c.msg.Content, "Synthesis: adopt the monorepo",
			"the reply must not re-fan as an ordinary stimulus — that is the reopen §D forbids")
	}
}

// TestSynthesisClose_ReplyTimeoutFallsBackToImmediateClose — the net: a chair
// that never replies (gate-suppressed, lease-denied, provider error) cannot
// leave the interaction open forever on an unattended channel. The timer runs
// the PR 4b-i immediate teardown with the ORIGINAL bounding stimulus as the
// closing message — on the floor path that stimulus was already delivered live
// inside its round, so the notification is stamped redelivery=true and the
// members skip the duplicate ingest (the Python half's contract).
func TestSynthesisClose_ReplyTimeoutFallsBackToImmediateClose(t *testing.T) {
	router, disp, ch, reader := synthesisCloseHarness(t, 2)
	router.synthesisTimeout = 5 * time.Millisecond

	tick(t, router, ch)
	tick(t, router, ch) // bound → synthesis turn dispatched, timer armed

	require.Eventually(t, func() bool {
		return closedCount(t, reader, structuralTrigger) == 1
	}, 2*time.Second, 2*time.Millisecond, "the timeout closes without the reply")
	// The teardown runs on the timer goroutine, so the notification fan's
	// WaitForPendingFanout registration races the metric above — poll the fan.
	require.Eventually(t, func() bool {
		return len(disp.closeNotifications()) > 0
	}, 2*time.Second, 2*time.Millisecond, "the fallback close still fans the artifact notification")

	assert.Equal(t, int64(1), synthesisTurnCount(t, reader, "closed_on_timeout"))
	notifications := disp.closeNotifications()
	require.NotEmpty(t, notifications)
	for _, c := range notifications {
		assert.Contains(t, c.msg.Content, "continue",
			"the fallback close carries the bounding stimulus, the 4b-i artifact shape")
		assert.True(t, c.env.InteractionCloseRedelivery,
			"the floor path delivered the stimulus live — the notification is a redelivery")
		assert.Equal(t, structuralTrigger, c.env.InteractionCloseTrigger)
	}
}

// TestSynthesisClose_NoChairFallsBackToImmediateClose — the 4b-i posture is the
// fallback, not a regression: with no escalation chair resolvable the bound
// closes immediately (every 4b-i test rides this branch; this pins it explicitly
// alongside the chair_missing outcome).
func TestSynthesisClose_NoChairFallsBackToImmediateClose(t *testing.T) {
	router, disp, ch, reader := synthesisCloseHarness(t, 2)
	router.SetEscalationChair(ch, "") // unset — runtime drift shape

	tick(t, router, ch)
	tick(t, router, ch)
	router.WaitForPendingFanout()

	assert.Equal(t, int64(1), closedCount(t, reader, structuralTrigger),
		"chairless bound closes immediately — termination never waits on a turn nobody will take")
	assert.Empty(t, disp.synthesisTurns())
	assert.Equal(t, int64(1), synthesisTurnCount(t, reader, "chair_missing"))
}

// TestSynthesisClose_StragglerWithheldWhileArmed — the armed window admits ONE
// message: the chair's claimed reply. Any other traffic (a straggler responder
// reply claiming the armed id, a fresh operator stimulus) is withheld — no
// floor round, no second synthesis turn, no close — and the chair's reply still
// lands the close afterwards.
func TestSynthesisClose_StragglerWithheldWhileArmed(t *testing.T) {
	router, disp, ch, reader := synthesisCloseHarness(t, 2)

	tick(t, router, ch)
	openID, _, _ := router.openInteractionEscalationState(ch)
	tick(t, router, ch) // bound → armed
	require.Len(t, disp.synthesisTurns(), 1)
	before := len(disp.snapshot())

	// A responder's straggler reply claiming the armed interaction…
	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: ch, SenderID: "ember-owl",
		Content:  "one more thought…",
		Metadata: map[string]any{"interaction_id": openID},
	}, ""))
	// …and a fresh unstamped operator stimulus land inside the armed window.
	tick(t, router, ch)
	router.WaitForPendingFanout()

	assert.Zero(t, closedCount(t, reader, structuralTrigger),
		"armed-window traffic neither closes nor revives the discussion")
	assert.Len(t, disp.synthesisTurns(), 1, "the synthesis turn is once-per-interaction")
	assert.Equal(t, before, len(disp.snapshot()),
		"withheld: committed history, but no dispatch fans into the terminating discussion")

	chairReply(t, router, ch, "iron-fox", openID, "Synthesis: converged.")
	router.WaitForPendingFanout()
	assert.Equal(t, int64(1), closedCount(t, reader, structuralTrigger),
		"the chair's reply still closes after the withheld window")
}

// TestSynthesisClose_ConcurrentPathWithholdsBoundingStimulusAndCloses — the
// concurrent path keeps its close-before-dispatch ordering under the synthesis
// flow: the bounding stimulus is withheld (no reply can re-fan), the synthesis
// turn dispatches, and the chair's reply is the sole-delivery closing artifact.
func TestSynthesisClose_ConcurrentPathWithholdsBoundingStimulusAndCloses(t *testing.T) {
	reader := sdkmetric.NewManualReader()
	mp := sdkmetric.NewMeterProvider(sdkmetric.WithReader(reader))
	t.Cleanup(func() { _ = mp.Shutdown(context.Background()) })
	closed, err := mp.Meter("test").Int64Counter("channel.conversation.interaction_closed")
	require.NoError(t, err)
	store := newTestStore(t, SQLiteOptions{})
	disp := &dispatchRecorder{}
	router := NewChannelRouter(store, disp, zap.NewNop(), &RouterMetrics{InteractionClosed: closed})
	ch := mustCreateGroupWithPolicies(t, store, "brainstorm",
		map[string]RespondPolicy{
			"operator":  RespondNever,
			"ember-owl": RespondAlways, // single responder → concurrent path
		}, "operator", "ember-owl")
	router.SetAutonomous(ch, AutonomousConfig{
		Enabled: true, MaxRounds: 1, Convener: "operator", Goal: "Converge.",
	})
	router.SetEscalationChair(ch, "ember-owl")

	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: ch, SenderID: "operator", Content: "the opener",
	}, "")) // round-1 guard: the opener is delivered live
	openID, _, _ := router.openInteractionEscalationState(ch)
	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: ch, SenderID: "operator", Content: "the bounding message",
	}, "")) // the bounding message: withheld, synthesis dispatched

	assert.Zero(t, closedCount(t, reader, structuralTrigger))
	require.Len(t, disp.synthesisTurns(), 1)
	for _, c := range disp.snapshot() {
		if c.env.SynthesisTurn || c.env.InteractionCloseNotification {
			continue
		}
		assert.NotEqual(t, "the bounding message", c.msg.Content,
			"the bounding stimulus is withheld on the concurrent path — no reply can re-fan")
	}

	// PR #718 review finding 8: the chair (ember-owl, the sole responder that
	// took the withheld bounding round) has a synthesis turn in flight, so its
	// "thinking" mark must survive the withhold seam's clear — the console must
	// not blank for the armed window.
	assert.Contains(t, router.ChannelActivity(ch), "ember-owl",
		"the chair's in-flight synthesis-turn presence mark survives the armed-window withhold")

	chairReply(t, router, ch, "ember-owl", openID, "Synthesis: done.")
	router.WaitForPendingFanout()
	assert.Equal(t, int64(1), closedCount(t, reader, structuralTrigger))
	notifications := disp.closeNotifications()
	require.NotEmpty(t, notifications)
	for _, c := range notifications {
		assert.False(t, c.env.InteractionCloseRedelivery, "sole delivery of the synthesis")
		assert.Contains(t, c.msg.Content, "Synthesis: done.")
	}
}

// TestSynthesisClose_HumanChannelUntouched — OQ #2: a chaired but NON-autonomous
// channel never dispatches a synthesis turn and never closes on a bound; the
// whole mechanism is scoped to `autonomous.enabled`.
func TestSynthesisClose_HumanChannelUntouched(t *testing.T) {
	router, disp, ch, reader := synthesisCloseHarness(t, 2)
	router.SetAutonomous(ch, AutonomousConfig{Enabled: false, MaxRounds: 2})

	tick(t, router, ch)
	tick(t, router, ch)
	tick(t, router, ch)
	router.WaitForPendingFanout()

	assert.Zero(t, closedCount(t, reader, structuralTrigger))
	assert.Empty(t, disp.synthesisTurns())
	assert.Empty(t, disp.closeNotifications())
}

// TestSynthesisClose_DirectiveComposition — the directive assembly: goal first,
// topic as the fallback subject, and a no-config channel still gets a
// synthesizable instruction (an armed channel needs topic/agenda/goal to
// convene, but the directive must never be empty — the chair's turn is the one
// mandatory §D artifact).
func TestSynthesisClose_DirectiveComposition(t *testing.T) {
	got := composeSynthesisDirective(AutonomousConfig{
		Goal: "A recommendation.", Topic: "Monorepos.",
	})
	assert.Contains(t, got, "Goal: A recommendation.")
	assert.Contains(t, got, "Topic: Monorepos.")
	assert.True(t, strings.Index(got, "Goal:") < strings.Index(got, "Topic:"),
		"the goal leads — the synthesis is judged against it")

	assert.NotEmpty(t, composeSynthesisDirective(AutonomousConfig{}),
		"a bare config still yields a synthesize-the-outcome directive")
}
