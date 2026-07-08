package channels

// standing_budget_test.go — RFC 0052 §E standing/scheduled discussions,
// orchestrator half (v0.3.11 PR 7b). TDD-first: pins the RUNTIME aggregate SPEND
// bound that activates PR 7a's (previously dark) `standing_budget_tokens` config
// gate — the SPEND twin of convening_counter_test.go's aggregate COUNT bound.
//
// PR 7a made an armed STANDING channel un-creatable without an aggregate bound
// (`autonomous.max_convenings` and/or `autonomous.standing_budget_tokens`,
// `ErrAutonomousStandingBoundRequired`); PR 7b-i activated the count half. This
// slice makes `standing_budget_tokens` a live ceiling: each interaction CLOSE
// folds its settled discussion spend into a per-channel running total
// ([ChannelRouter.recordStandingSpend], wired at the markInteractionClosed
// close-notification seam), and [ChannelRouter.ConveneChannel] refuses a fresh
// convening once that total reaches the budget ([ErrAutonomousStandingBudgetExhausted],
// 429 — the sibling of the count ceiling's [ErrAutonomousConveningBoundReached]).
//
// The total is process-lifetime in-memory state (the sibling of the convening
// count): a restart resets it, it does NOT reset on disarm/re-arm within a
// process (the conservative aggregate-safety posture), and a channel DELETE
// clears it (no map leak). It is folded from the wallet's per-interaction running
// total at close, so a $0/mock fleet with no wallet wired leaves the gate inert.

import (
	"context"
	"sync"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// mapSpender is a per-interaction-id stand-in for the wallet's running total
// ([interactionSpender]) — unlike bounded_close_test.go's fixed fakeSpender it
// reports a distinct spend per id, so the fold-wiring test can prove the fold is
// keyed on the CLOSING interaction and folds nothing for an unknown id.
type mapSpender map[string]int64

func (m mapSpender) InteractionSpend(id string) int64 { return m[id] }

// standingArmedBudget is the resolved block a standing channel bounded ONLY by a
// token budget carries: a convener, a subject, a schedule, a positive
// `standing_budget_tokens`, and NO count bound (max_convenings=0) — so the spend
// gate is the sole aggregate ceiling under test, isolated from the count gate.
func standingArmedBudget(budget int64) AutonomousConfig {
	a := standingArmed(0) // convener/topic/goal/schedule, max_convenings unset.
	a.StandingBudgetTokens = budget
	return a
}

// TestConvene_RefusedWhenStandingSpendReachesBudget — the aggregate SPEND ceiling:
// once the folded across-window spend reaches `standing_budget_tokens`, a fresh
// convening is refused with ErrAutonomousStandingBudgetExhausted, dispatching
// nothing. At-or-over is refused (a spend EQUAL to the budget is exhausted, the
// `>=` boundary), the fail-closed direction.
func TestConvene_RefusedWhenStandingSpendReachesBudget(t *testing.T) {
	disp := &messageRecordingDispatcher{}
	router, ch := conveningHarness(t, disp, standingArmedBudget(10_000))

	// Fold spend up to exactly the budget — the boundary case.
	router.recordStandingSpend(ch, 10_000)

	_, err := router.ConveneChannel(context.Background(), ch)
	require.Error(t, err, "spend at the budget exhausts it")
	assert.ErrorIs(t, err, ErrAutonomousStandingBudgetExhausted)
	assert.Empty(t, conveneEnvelopes(disp), "an exhausted-budget convene dispatches no opener")
}

// TestConvene_AllowedBelowStandingBudget — spend strictly under the budget still
// admits a convening: the gate refuses only at-or-over, never a channel with
// headroom left.
func TestConvene_AllowedBelowStandingBudget(t *testing.T) {
	disp := &messageRecordingDispatcher{}
	router, ch := conveningHarness(t, disp, standingArmedBudget(10_000))

	router.recordStandingSpend(ch, 9_999) // one token of headroom remains.

	_, err := router.ConveneChannel(context.Background(), ch)
	require.NoError(t, err, "spend below the budget leaves headroom to convene")
	assert.Len(t, conveneEnvelopes(disp), 1)
}

// TestConvene_StandingBudgetZeroIsUnbounded — `standing_budget_tokens` unset (0)
// leaves the spend check off: a one-shot channel (or a standing channel bounded
// only by max_convenings) is never gated on spend, however much it has folded.
// The spend is still tracked, for the readout a later slice surfaces.
func TestConvene_StandingBudgetZeroIsUnbounded(t *testing.T) {
	disp := &messageRecordingDispatcher{}
	router, ch := conveningHarness(t, disp, standingArmedBudget(0))

	router.recordStandingSpend(ch, 1_000_000) // far beyond any realistic budget.

	for i := 0; i < 3; i++ {
		_, err := router.ConveneChannel(context.Background(), ch)
		require.NoErrorf(t, err, "convening %d is unbounded with standing_budget_tokens=0", i+1)
	}
	assert.Len(t, conveneEnvelopes(disp), 3)
	assert.EqualValues(t, 1_000_000, router.StandingSpend(ch), "spend is tracked even when unbounded")
}

// TestRecordStandingSpend_AdditiveAcrossCloses — the running total is additive
// across interaction closes, and a non-positive fold (an untracked/zero-spend
// interaction, or a nil-wallet read) is a no-op, so the total only ever grows by
// real spend.
func TestRecordStandingSpend_AdditiveAcrossCloses(t *testing.T) {
	router, ch := conveningHarness(t, &messageRecordingDispatcher{}, standingArmedBudget(0))

	router.recordStandingSpend(ch, 3_000)
	router.recordStandingSpend(ch, 2_000)
	assert.EqualValues(t, 5_000, router.StandingSpend(ch), "closes accumulate")

	router.recordStandingSpend(ch, 0)
	router.recordStandingSpend(ch, -100)
	assert.EqualValues(t, 5_000, router.StandingSpend(ch), "a non-positive fold is a no-op")
}

// TestStandingSpend_ZeroForUnconvenedChannel — the accessor reports 0 for a
// channel that has never folded a close (the readout's baseline).
func TestStandingSpend_ZeroForUnconvenedChannel(t *testing.T) {
	router, ch := conveningHarness(t, &messageRecordingDispatcher{}, standingArmedBudget(10_000))
	assert.Zero(t, router.StandingSpend(ch))
}

// TestMarkInteractionClosed_FoldsStandingSpend — the fold WIRING: a deliberate
// interaction close reads the closing interaction's settled running total from
// the wallet and folds it into the channel's standing spend. A SECOND (stale)
// close of the same id — the losing side of a two-closers race — does NOT
// double-fold: the fold rides the open→retired transition, which fires exactly
// once per closed id.
func TestMarkInteractionClosed_FoldsStandingSpend(t *testing.T) {
	router, ch := conveningHarness(t, &messageRecordingDispatcher{}, standingArmedBudget(0))
	id, _, commit, _ := router.resolveInteractionID(context.Background(), ch, ChannelTypeGroup, "")
	commit(true) // open and committed.
	router.SetInteractionSpender(mapSpender{id: 4_200})

	router.markInteractionClosed(ch, id, structuralTrigger)
	require.EqualValues(t, 4_200, router.StandingSpend(ch), "the close folds the interaction's spend")

	router.markInteractionClosed(ch, id, structuralTrigger)
	assert.EqualValues(t, 4_200, router.StandingSpend(ch), "a stale re-close does not double-fold")
}

// TestMarkInteractionClosed_NilSpenderFoldsNothing — with no wallet wired (a
// $0/mock fleet, or a unit fleet), the close folds nothing and the standing gate
// stays inert — the same posture as the bounded-close soft-budget trigger.
func TestMarkInteractionClosed_NilSpenderFoldsNothing(t *testing.T) {
	router, ch := conveningHarness(t, &messageRecordingDispatcher{}, standingArmedBudget(0))
	id, _, commit, _ := router.resolveInteractionID(context.Background(), ch, ChannelTypeGroup, "")
	commit(true)
	// no SetInteractionSpender — r.spend is nil.

	router.markInteractionClosed(ch, id, structuralTrigger)
	assert.Zero(t, router.StandingSpend(ch), "a nil wallet folds nothing")
}

// TestRecordStandingSpend_ConcurrentFoldsAreRaceFree — folds land from
// independent interaction closes (each on its own goroutine) while the gate reads
// the total; under -race this proves standingSpend is touched only under
// standingMu, and the accumulator loses no fold (the sum is exact — additive, not
// a check-and-mutate the convening count's atomicity guards).
func TestRecordStandingSpend_ConcurrentFoldsAreRaceFree(t *testing.T) {
	router, ch := conveningHarness(t, &messageRecordingDispatcher{}, standingArmedBudget(0))

	const goroutines = 16
	var wg sync.WaitGroup
	for i := 0; i < goroutines; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			router.recordStandingSpend(ch, 1_000)
			_ = router.StandingSpend(ch) // concurrent read of the same total.
		}()
	}
	wg.Wait()

	assert.EqualValues(t, goroutines*1_000, router.StandingSpend(ch),
		"every concurrent fold is accounted for")
}

// TestPurgeChannelInteraction_ClearsStandingSpend — deleting a channel drops its
// standing spend with the rest of its resolver state, so the map does not leak
// one entry per deleted standing channel (the sibling of the convening-count
// clear).
func TestPurgeChannelInteraction_ClearsStandingSpend(t *testing.T) {
	router, ch := conveningHarness(t, &messageRecordingDispatcher{}, standingArmedBudget(10_000))
	router.recordStandingSpend(ch, 5_000)
	require.EqualValues(t, 5_000, router.StandingSpend(ch))

	router.PurgeChannelInteraction(ch)
	assert.Zero(t, router.StandingSpend(ch), "channel delete clears the standing spend")
}
