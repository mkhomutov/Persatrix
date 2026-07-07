package channels

// convener_cadence_test.go — RFC 0052 §C anti-collapse cadence, PR 6
// (docs/rfcs/0052-pr-plan.md). TDD-first: this matrix was written red against
// the planned API and pins the convener half of the human-free keep-alive
// pressure —
//
//   - the per-agenda-item ration STATE MACHINE ([ChannelRouter.claimConvenerCadence]):
//     the CE5 one-escalation-per-interaction ration generalized to one turn per
//     agenda item, with the best-effort liveness re-invite, the monotonic cursor
//     loop guard ("never twice into silence on the same item"), and the
//     agenda-exhausted fall-through to the shipped chair escalation;
//   - the FANOUT-TAIL precedence ([ChannelRouter.maybeAdvanceAgenda]): a stalled
//     autonomous floor round dispatches ONE convener forced turn (reusing the §B
//     convene lane) while the agenda has items, and defers to the chair only when
//     the agenda is exhausted;
//   - the OQ #2 SCOPE gate: a human channel is byte-for-byte unchanged — the
//     cadence hook returns before touching state and the shipped chair escalation
//     fires exactly as before.

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
	"go.uber.org/zap"
)

// convenerAdvanceCount returns the convener_advance counter value for the given
// channel_type + outcome pair, 0 when absent — the sibling of
// [chairEscalationCount].
func convenerAdvanceCount(t *testing.T, rm metricdata.ResourceMetrics, channelType, outcome string) int64 {
	t.Helper()
	for _, sm := range rm.ScopeMetrics {
		for _, m := range sm.Metrics {
			if m.Name != "channel.conversation.convener_advance" {
				continue
			}
			sum, ok := m.Data.(metricdata.Sum[int64])
			require.Truef(t, ok, "convener_advance: expected Sum[int64], got %T", m.Data)
			for _, dp := range sum.DataPoints {
				ct, _ := dp.Attributes.Value("channel_type")
				oc, _ := dp.Attributes.Value("outcome")
				if ct.AsString() == channelType && oc.AsString() == outcome {
					return dp.Value
				}
			}
		}
	}
	return 0
}

// cadenceHarness builds a floor-controlled stall stage on an ARMED autonomous
// channel: a human stimulus author, a convener, a responder, and the escalation
// chair — every persona seat RespondAlways so a published stimulus grants a real
// floor round that stalls (the recorder never replies). Both the convener_advance
// and chair_escalation counters ride a manual reader so a test can pin which
// mechanism fired. `agenda` sets the RFC 0052 agenda the cadence advances through.
func cadenceHarness(t *testing.T, agenda []string) (*ChannelRouter, *dispatchRecorder, string, *sdkmetric.ManualReader) {
	t.Helper()
	disp := &dispatchRecorder{}
	router, ch, reader := cadenceHarnessWith(t, agenda, disp)
	return router, disp, ch, reader
}

// cadenceHarnessWith is cadenceHarness with the dispatcher injected, so a test
// can supply one that FAILS the convene-lane send (the dispatch_error branch) in
// place of the recorder. Every other test reaches it through cadenceHarness.
func cadenceHarnessWith(t *testing.T, agenda []string, disp MessageDispatcher) (*ChannelRouter, string, *sdkmetric.ManualReader) {
	t.Helper()
	reader := sdkmetric.NewManualReader()
	mp := sdkmetric.NewMeterProvider(sdkmetric.WithReader(reader))
	t.Cleanup(func() { _ = mp.Shutdown(context.Background()) })
	advance, err := mp.Meter("test").Int64Counter("channel.conversation.convener_advance")
	require.NoError(t, err)
	chair, err := mp.Meter("test").Int64Counter("channel.conversation.chair_escalation")
	require.NoError(t, err)
	delivered, err := mp.Meter("test").Int64Counter("channel.messages.delivered")
	require.NoError(t, err)
	store := newTestStore(t, SQLiteOptions{})
	router := NewChannelRouter(store, disp, zap.NewNop(), &RouterMetrics{
		ConvenerAdvance: advance, ChairEscalation: chair, MessagesDelivered: delivered,
	})
	ch := mustCreateGroupWithPolicies(t, store, "brainstorm",
		map[string]RespondPolicy{
			"alex":         RespondNever,  // the human stimulus author (no discussion seat)
			"nova-sparrow": RespondAlways, // the convener
			"ember-owl":    RespondAlways, // a responder (the open-floor audience)
			"iron-fox":     RespondAlways, // the escalation chair
		}, "alex", "nova-sparrow", "ember-owl", "iron-fox")
	router.SetFloorControl(ch, true, time.Millisecond)
	router.SetAutonomous(ch, AutonomousConfig{
		Enabled: true, MaxRounds: 50, Convener: "nova-sparrow",
		Topic: "Should we adopt a monorepo?", Agenda: agenda,
		Goal: "A synthesized recommendation.",
	})
	router.SetEscalationChair(ch, "iron-fox")
	return router, ch, reader
}

// convenerFailingDispatcher records like dispatchRecorder but FAILS every
// convene-lane send, exercising the maybeAdvanceAgenda dispatch_error branch (the
// convener's endpoint is unreachable); non-convene traffic still succeeds.
type convenerFailingDispatcher struct {
	*dispatchRecorder
}

func (d *convenerFailingDispatcher) Dispatch(ctx context.Context, env DispatchEnvelope, msg ChannelMessage) error {
	_ = d.dispatchRecorder.Dispatch(ctx, env, msg)
	if env.Convene {
		return errors.New("convener endpoint unreachable")
	}
	return nil
}

// stallOnce publishes one open-floor stimulus from the human author; with the
// recorder never replying, every granted floor turn times out and the round
// stalls by construction — the fanout tail then runs the cadence.
func stallOnce(t *testing.T, router *ChannelRouter, ch string) {
	t.Helper()
	require.NoError(t, router.Publish(context.Background(), ChannelMessage{
		ID: uuid.NewString(), ChannelID: ch, SenderID: "alex", Content: "thoughts, team?",
	}, ""))
}

// convenerAdvances filters the recorder's calls down to the cadence forced turns
// — the convene-lane dispatches to the convener (the opening convene is never
// issued in these tests, so every Convene envelope is a cadence turn).
func convenerAdvances(disp *dispatchRecorder) []recordedDispatch {
	var out []recordedDispatch
	for _, c := range disp.snapshot() {
		if c.env.Convene {
			out = append(out, c)
		}
	}
	return out
}

func chairEscalations(disp *dispatchRecorder) []recordedDispatch {
	var out []recordedDispatch
	for _, c := range disp.snapshot() {
		if c.env.ChairEscalation {
			out = append(out, c)
		}
	}
	return out
}

// setCommittedEntry installs a committed open-interaction entry for `ch`, letting
// a test drive [ChannelRouter.claimConvenerCadence] / [ChannelRouter.recordAgendaProgress]
// directly against a controlled cursor/liveness state (the resolver tests' poke
// pattern). In-package, guarded by interactionMu like every entry writer.
func setCommittedEntry(r *ChannelRouter, ch, id string, mutate func(*openInteraction)) {
	r.interactionMu.Lock()
	defer r.interactionMu.Unlock()
	e := &openInteraction{id: id, idCommitted: true}
	if mutate != nil {
		mutate(e)
	}
	r.openInteractions[ch] = e
}

// --- state machine (claimConvenerCadence / recordAgendaProgress) -------------

// TestClaimConvenerCadence_ReinviteThenAdvance — the per-item arc on an
// UNdiscussed item: the first claim RE-INVITES the current item (the liveness
// target's second chance), the second ADVANCES to the next item with a fresh
// ration.
func TestClaimConvenerCadence_ReinviteThenAdvance(t *testing.T) {
	router, _, ch, _ := cadenceHarness(t, []string{"A", "B"})
	setCommittedEntry(router, ch, "int-1", nil) // cursor 0, undiscussed, un-reinvited

	id, item, action, ok := router.claimConvenerCadence(ch, "", 2)
	require.True(t, ok)
	assert.Equal(t, "int-1", id)
	assert.Equal(t, 0, item, "an undiscussed item is re-invited in place, not skipped")
	assert.Equal(t, cadenceReinvite, action)

	id, item, action, ok = router.claimConvenerCadence(ch, "", 2)
	require.True(t, ok)
	assert.Equal(t, "int-1", id)
	assert.Equal(t, 1, item, "the second stall advances to the next item")
	assert.Equal(t, cadenceAdvance, action)
}

// TestClaimConvenerCadence_DiscussedItemAdvancesWithoutReinvite — the liveness
// target only spends a re-invite on an UNDER-discussed item: an item that has
// drawn a substantive round advances straight away, no wasted re-invite.
func TestClaimConvenerCadence_DiscussedItemAdvancesWithoutReinvite(t *testing.T) {
	router, _, ch, _ := cadenceHarness(t, []string{"A", "B"})
	setCommittedEntry(router, ch, "int-1", func(e *openInteraction) {
		e.agendaItemDiscussed = true
	})

	_, item, action, ok := router.claimConvenerCadence(ch, "", 2)
	require.True(t, ok)
	assert.Equal(t, 1, item)
	assert.Equal(t, cadenceAdvance, action, "a discussed item is not re-invited")
}

// TestRecordAgendaProgress_MarksCurrentItemDiscussed — a working round records
// discussion on the current item, which then suppresses that item's re-invite.
func TestRecordAgendaProgress_MarksCurrentItemDiscussed(t *testing.T) {
	router, _, ch, _ := cadenceHarness(t, []string{"A", "B"})
	setCommittedEntry(router, ch, "int-1", nil)

	router.recordAgendaProgress(ch, "")
	_, item, action, ok := router.claimConvenerCadence(ch, "", 2)
	require.True(t, ok)
	assert.Equal(t, 1, item)
	assert.Equal(t, cadenceAdvance, action, "recorded progress skips the re-invite")
}

// TestClaimConvenerCadence_ExhaustedFallsThrough — on the LAST item with its
// ration spent, the claim reports not-ok so the caller falls through to the
// chair escalation (§D converge). This is the loop guard's terminal: the cursor
// never advances past the final item.
func TestClaimConvenerCadence_ExhaustedFallsThrough(t *testing.T) {
	router, _, ch, _ := cadenceHarness(t, []string{"A", "B"})
	setCommittedEntry(router, ch, "int-1", func(e *openInteraction) {
		e.agendaCursor = 1 // last item of a 2-item agenda
		e.agendaItemReinvited = true
	})

	_, _, _, ok := router.claimConvenerCadence(ch, "", 2)
	assert.False(t, ok, "an exhausted agenda falls through to the chair")
}

// TestClaimConvenerCadence_AgendaShrunkBelowCursorFallsThrough — a live RFC 0050
// apply can shrink `autonomous.agenda` mid-discussion while the cursor rides the
// resolver entry; a cursor now at/past the new length must fall through to the
// chair, never index the fresh agenda out of range (composeAgendaAdvanceDirective).
func TestClaimConvenerCadence_AgendaShrunkBelowCursorFallsThrough(t *testing.T) {
	router, _, ch, _ := cadenceHarness(t, []string{"A", "B", "C"})
	setCommittedEntry(router, ch, "int-1", func(e *openInteraction) {
		e.agendaCursor = 2 // was the 3rd item...
	})

	// ...but the agenda has since been shrunk to a single item (length 1).
	_, _, _, ok := router.claimConvenerCadence(ch, "", 1)
	assert.False(t, ok, "a cursor past a shrunken agenda yields to the chair, no panic")
}

// TestClaimConvenerCadence_NoAgendaFallsThrough — a single-topic discussion (no
// agenda) has no convener cadence; the chair converges (RFC §C agenda scope).
func TestClaimConvenerCadence_NoAgendaFallsThrough(t *testing.T) {
	router, _, ch, _ := cadenceHarness(t, nil)
	setCommittedEntry(router, ch, "int-1", nil)

	_, _, _, ok := router.claimConvenerCadence(ch, "", 0)
	assert.False(t, ok, "no agenda means no cadence")
}

// TestClaimConvenerCadence_DivergenceGuard — a stall that outlived its
// interaction (its stamped id no longer matches the open one) must not spend the
// successor's ration, mirroring maybeEscalateStall / advanceBoundedCloseRound.
func TestClaimConvenerCadence_DivergenceGuard(t *testing.T) {
	router, _, ch, _ := cadenceHarness(t, []string{"A", "B"})
	setCommittedEntry(router, ch, "int-1", nil)

	_, _, _, ok := router.claimConvenerCadence(ch, "int-STALE", 2)
	assert.False(t, ok, "a diverged stall does not advance the successor")
}

// TestClaimConvenerCadence_MonotonicCursorNeverRepeats — the loop guard end to
// end: walking a 3-item agenda through repeated stalls poses each item AT MOST
// once as an advance target and the cursor is strictly monotonic — it never
// re-poses an item it advanced past.
func TestClaimConvenerCadence_MonotonicCursorNeverRepeats(t *testing.T) {
	router, _, ch, _ := cadenceHarness(t, []string{"A", "B", "C"})
	setCommittedEntry(router, ch, "int-1", func(e *openInteraction) {
		e.agendaItemDiscussed = true // skip re-invites so advances are isolated
	})

	var advancedTo []int
	for {
		_, item, action, ok := router.claimConvenerCadence(ch, "", 3)
		if !ok {
			break
		}
		// discussed resets to false on advance, so re-mark it to keep isolating
		// the advance ladder (the liveness re-invite is covered elsewhere).
		router.recordAgendaProgress(ch, "")
		if action == cadenceAdvance {
			advancedTo = append(advancedTo, item)
		}
	}
	assert.Equal(t, []int{1, 2}, advancedTo,
		"a 3-item agenda advances to items 1 and 2 exactly once each, then exhausts")
}

// --- fanout-tail behaviour (maybeAdvanceAgenda) ------------------------------

// TestConvenerCadence_StalledRoundAdvancesAgenda — the arc through the real
// fanout: repeated stalls on a 2-item agenda dispatch convener forced turns
// (re-invite item 0, advance to item 1, re-invite item 1) down the convene lane,
// each addressed to the convener and naming the item to (re-)pose; the chair
// escalation is SUPPRESSED while the agenda has items. When the agenda exhausts,
// the shipped chair escalation fires — exactly once, to the chair.
func TestConvenerCadence_StalledRoundAdvancesAgenda(t *testing.T) {
	router, disp, ch, reader := cadenceHarness(t, []string{"Build tooling cost", "Migration effort"})

	stallOnce(t, router, ch) // stall 1 → re-invite item 0
	require.Len(t, convenerAdvances(disp), 1)
	assert.Empty(t, chairEscalations(disp), "the chair is suppressed while the agenda has items")
	first := convenerAdvances(disp)[0]
	assert.Equal(t, "nova-sparrow", first.env.Recipient.ParticipantID, "the convener advances the agenda")
	assert.Contains(t, first.msg.Content, "Build tooling cost")

	stallOnce(t, router, ch) // stall 2 → advance to item 1
	require.Len(t, convenerAdvances(disp), 2)
	assert.Contains(t, convenerAdvances(disp)[1].msg.Content, "Migration effort")

	stallOnce(t, router, ch) // stall 3 → re-invite item 1
	require.Len(t, convenerAdvances(disp), 3)
	assert.Contains(t, convenerAdvances(disp)[2].msg.Content, "Migration effort")
	assert.Empty(t, chairEscalations(disp), "still no chair escalation before exhaustion")

	stallOnce(t, router, ch) // stall 4 → agenda exhausted → chair converges
	assert.Len(t, convenerAdvances(disp), 3, "no further convener turn once exhausted")
	require.Len(t, chairEscalations(disp), 1, "the chair escalation fires on exhaustion")
	assert.Equal(t, "iron-fox", chairEscalations(disp)[0].env.Recipient.ParticipantID)

	var rm metricdata.ResourceMetrics
	require.NoError(t, reader.Collect(context.Background(), &rm))
	assert.Equal(t, int64(2), convenerAdvanceCount(t, rm, "group", "reinvite"))
	assert.Equal(t, int64(1), convenerAdvanceCount(t, rm, "group", "advance"))
	assert.Equal(t, int64(1), chairEscalationCount(t, rm, "group", "dispatched"))
}

// TestConvenerCadence_AdvanceTurnRidesConveneLane — the anti-collapse turn reuses
// the §B convene wire lane end to end (no new proto field / prompt): the forced
// turn carries the `convene` marker (gate admit + format_convener_opening) and NO
// chair-escalation marker, is stamped with the open interaction id (lease
// attribution + in-interaction reply), and comes from the synthetic convene
// sender so it never self-suppresses at the receiver gate.
func TestConvenerCadence_AdvanceTurnRidesConveneLane(t *testing.T) {
	router, disp, ch, _ := cadenceHarness(t, []string{"A", "B"})
	stallOnce(t, router, ch)

	turns := convenerAdvances(disp)
	require.Len(t, turns, 1)
	env, msg := turns[0].env, turns[0].msg
	assert.True(t, env.Convene, "the cadence turn rides the convene marker lane")
	assert.False(t, env.ChairEscalation, "it is NOT a chair escalation")
	assert.Equal(t, ConveneDispatchSenderID, msg.SenderID, "the synthetic convene sender never self-suppresses")
	assert.NotEmpty(t, readInteractionID(msg.Metadata), "stamped with the open interaction id for lease attribution")
}

// TestConvenerCadence_HumanChannelUnchanged — the OQ #2 scope invariant: on a
// human (non-autonomous) channel the cadence hook is inert — a stalled round
// dispatches NO convener turn and the shipped chair escalation fires exactly as
// before, proving the CE5 one-shot ration is unchanged off the autonomous path.
func TestConvenerCadence_HumanChannelUnchanged(t *testing.T) {
	router, disp, ch, reader := cadenceHarness(t, []string{"A", "B"})
	router.SetAutonomous(ch, AutonomousConfig{Enabled: false}) // disarm → ordinary channel

	stallOnce(t, router, ch)

	assert.Empty(t, convenerAdvances(disp), "no convener cadence on a human channel")
	require.Len(t, chairEscalations(disp), 1, "the shipped chair escalation fires unchanged")
	assert.Equal(t, "iron-fox", chairEscalations(disp)[0].env.Recipient.ParticipantID)

	var rm metricdata.ResourceMetrics
	require.NoError(t, reader.Collect(context.Background(), &rm))
	assert.Equal(t, int64(0), convenerAdvanceCount(t, rm, "group", "advance"))
	assert.Equal(t, int64(0), convenerAdvanceCount(t, rm, "group", "reinvite"))
	assert.Equal(t, int64(1), chairEscalationCount(t, rm, "group", "dispatched"))
}

// TestConvenerCadence_WorkingRoundNoTurn — a round that drew a reply is not a
// stall: no convener turn fires, and the current item is recorded as discussed
// so its later stall advances without a wasted re-invite. Driven at the seam
// (maybeAdvanceAgenda) with a working-round outcome.
func TestConvenerCadence_WorkingRoundNoTurn(t *testing.T) {
	router, disp, ch, _ := cadenceHarness(t, []string{"A", "B"})
	setCommittedEntry(router, ch, "int-1", nil)
	members, err := router.store.GetMembers(context.Background(), ch)
	require.NoError(t, err)

	handled := router.maybeAdvanceAgenda(context.Background(),
		ChannelMessage{ChannelID: ch, Metadata: map[string]any{"interaction_id": "int-1"}},
		ChannelTypeGroup, floorRoundOutcome{granted: 2, replied: 1}, members, len(members),
		router.AutonomousFor(ch))

	assert.False(t, handled, "a working round is not handled by the cadence")
	assert.Empty(t, convenerAdvances(disp), "no convener turn on a replied round")

	// the working round marked the item discussed → its next stall advances.
	_, item, action, ok := router.claimConvenerCadence(ch, "int-1", 2)
	require.True(t, ok)
	assert.Equal(t, 1, item)
	assert.Equal(t, cadenceAdvance, action)
}

// TestConvenerCadence_BrokenConvenerFallsThroughWithoutSpending — the guard the
// PR documents ("a broken convener neither spends a ration nor moves the
// cursor", checked BEFORE the claim): a convener that has drifted out of the
// roster, or is an observer (respond: never), cannot author the forced turn — a
// receiver-gate self-suppress the convene path also refuses — so maybeAdvanceAgenda
// falls through to the chair WITHOUT dispatching or spending the per-item ration.
func TestConvenerCadence_BrokenConvenerFallsThroughWithoutSpending(t *testing.T) {
	stalled := floorRoundOutcome{granted: 2, replied: 0}
	cases := []struct {
		name  string
		munge func([]Member) []Member // the roster the fanout would hand the hook
	}{
		{"drifted (non-member)", func(m []Member) []Member {
			var out []Member
			for _, mem := range m {
				if mem.ParticipantID != "nova-sparrow" {
					out = append(out, mem)
				}
			}
			return out
		}},
		{"observer (respond: never)", func(m []Member) []Member {
			for i := range m {
				if m[i].ParticipantID == "nova-sparrow" {
					m[i].RespondPolicy = RespondNever
				}
			}
			return m
		}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			router, disp, ch, _ := cadenceHarness(t, []string{"A", "B"})
			setCommittedEntry(router, ch, "int-1", nil)
			members, err := router.store.GetMembers(context.Background(), ch)
			require.NoError(t, err)
			roster := tc.munge(members)

			handled := router.maybeAdvanceAgenda(context.Background(),
				ChannelMessage{ChannelID: ch, Metadata: map[string]any{"interaction_id": "int-1"}},
				ChannelTypeGroup, stalled, roster, len(roster), router.AutonomousFor(ch))

			assert.False(t, handled, "a broken convener falls through to the chair")
			assert.Empty(t, convenerAdvances(disp), "no convener turn dispatched")

			// the ration is pristine: a real claim still RE-INVITES item 0 (cursor
			// unmoved, un-reinvited) — nothing was spent on the broken convener.
			_, item, action, ok := router.claimConvenerCadence(ch, "int-1", 2)
			require.True(t, ok)
			assert.Equal(t, 0, item, "the cursor did not move")
			assert.Equal(t, cadenceReinvite, action, "the per-item ration was not spent")
		})
	}
}

// TestConvenerCadence_DispatchErrorSpendsRationSuppressesChair — a FAILED
// convene-lane send (the convener's endpoint is unreachable) is metered
// `dispatch_error` and STILL reports handled: the ration is spent (claimed before
// the send) and the chair must NOT also fire into the same silence while the
// agenda has items. Mirrors the chair's own chair-gone branch — one attempt per
// item, no refund — so the next stall ADVANCES rather than re-inviting forever.
func TestConvenerCadence_DispatchErrorSpendsRationSuppressesChair(t *testing.T) {
	rec := &dispatchRecorder{}
	router, ch, reader := cadenceHarnessWith(t, []string{"A", "B"},
		&convenerFailingDispatcher{dispatchRecorder: rec})

	stallOnce(t, router, ch) // stall 1 → re-invite item 0, send FAILS

	assert.Empty(t, chairEscalations(rec), "a failed convener dispatch still suppresses the chair")
	var rm metricdata.ResourceMetrics
	require.NoError(t, reader.Collect(context.Background(), &rm))
	assert.Equal(t, int64(1), convenerAdvanceCount(t, rm, "group", "dispatch_error"))
	assert.Equal(t, int64(0), convenerAdvanceCount(t, rm, "group", "reinvite"),
		"a failed send is metered dispatch_error, not reinvite")

	stallOnce(t, router, ch) // stall 2 → item 0's ration was spent, so this ADVANCES

	turns := convenerAdvances(rec)
	require.Len(t, turns, 2)
	assert.Contains(t, turns[0].msg.Content, "A", "stall 1 re-invited item 0")
	assert.Contains(t, turns[1].msg.Content, "B",
		"stall 2 advanced to item 1 — item 0's failed re-invite was not refunded")
	assert.Empty(t, chairEscalations(rec), "still no chair escalation while the agenda has items")
}
