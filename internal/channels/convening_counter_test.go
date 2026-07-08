package channels

// convening_counter_test.go — RFC 0052 §E standing/scheduled discussions,
// orchestrator half (v0.3.11 PR 7b). TDD-first: pins the RUNTIME aggregate
// convening bound that activates PR 7a's (previously dark) config gate.
//
// PR 7a made an armed STANDING channel un-creatable without an aggregate bound
// (`autonomous.max_convenings` and/or `autonomous.standing_budget_tokens`,
// `ErrAutonomousStandingBoundRequired`) but nothing ENFORCED that bound at
// convene time — the count was neither tracked nor consulted. This slice makes
// `max_convenings` a live ceiling: [ChannelRouter.ConveneChannel] counts each
// SUCCESSFUL convening and refuses once the aggregate count is reached
// ([ErrAutonomousConveningBoundReached], 429), for BOTH the manual convene path
// (today) and the scheduled timer-fired path (the next PR 7b slice). A convene
// that MISSES (dispatch failure) does not consume a slot — the count reflects
// convenings that actually happened, so a flapping convener endpoint never
// silently burns the aggregate budget.
//
// The counter is process-lifetime in-memory state (the sibling of every other
// per-channel router registry): a restart resets it, exactly as the wallet's
// per-interaction accounting resets. It does NOT reset on disarm/re-arm within a
// process — the conservative aggregate-safety posture (re-arming must not refill
// the convening budget); a channel DELETE clears it (no map leak).
//
// `standing_budget_tokens` aggregate-spend enforcement, the config-round-trip
// timer seam that fires the schedule, and the web convening-count readout are
// the remaining PR 7b slices; this slice lands the count bound (the simpler,
// self-contained half of the aggregate gate) first, so the timer seam never
// wires up auto-convening ahead of the ceiling that bounds it.

import (
	"context"
	"errors"
	"sync"
	"sync/atomic"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
)

// conveningHarness mirrors conveneHarness but takes the dispatcher, so a test
// can supply a failing one to exercise the release-on-miss path.
func conveningHarness(t *testing.T, disp MessageDispatcher, a AutonomousConfig) (*ChannelRouter, string) {
	t.Helper()
	store := newTestStore(t, SQLiteOptions{})
	router := NewChannelRouter(store, disp, zap.NewNop(), nil)
	ch := mustCreateGroupWithPolicies(t, store, "planning",
		map[string]RespondPolicy{
			"nova-sparrow": RespondAlways, // the convener
			"ember-owl":    RespondAlways,
			"iron-fox":     RespondAlways, // the chair
		}, "nova-sparrow", "ember-owl", "iron-fox")
	router.SetAutonomous(ch, a)
	router.SetEscalationChair(ch, "iron-fox")
	return router, ch
}

// standingArmed is the resolved block a standing channel carries: a convener, a
// subject, and the aggregate count bound under test.
func standingArmed(maxConvenings int) AutonomousConfig {
	return AutonomousConfig{
		Enabled:                 true,
		Convener:                "nova-sparrow",
		Topic:                   "Weekly architecture review",
		Goal:                    "A synthesized recommendation.",
		ScheduleIntervalSeconds: 3600,
		MaxConvenings:           maxConvenings,
	}
}

// TestConvene_CountsEachConveningTowardMaxConvenings — the aggregate ceiling:
// with max_convenings=2, the first two convenings succeed and the third is
// refused with ErrAutonomousConveningBoundReached, dispatching nothing. (The
// recording dispatcher never replies, so no interaction ever commits — this
// drives the counter directly, independent of the orthogonal already-convening
// guard.)
func TestConvene_CountsEachConveningTowardMaxConvenings(t *testing.T) {
	disp := &messageRecordingDispatcher{}
	router, ch := conveningHarness(t, disp, standingArmed(2))

	_, err := router.ConveneChannel(context.Background(), ch)
	require.NoError(t, err, "1st convening is under the bound")
	_, err = router.ConveneChannel(context.Background(), ch)
	require.NoError(t, err, "2nd convening reaches the bound")
	assert.Equal(t, 2, router.ConveningCount(ch))

	_, err = router.ConveneChannel(context.Background(), ch)
	require.Error(t, err, "3rd convening exceeds max_convenings")
	assert.ErrorIs(t, err, ErrAutonomousConveningBoundReached)

	assert.Len(t, conveneEnvelopes(disp), 2,
		"only the two bound-fitting convenings dispatch an opener")
	assert.Equal(t, 2, router.ConveningCount(ch),
		"a refused convening does not advance the count")
}

// TestConvene_MaxConveningsZeroIsUnbounded — max_convenings unset (0) leaves the
// count check off: a one-shot armed channel (or a standing channel bounded only
// by standing_budget_tokens) is never gated on the count. The count is still
// tracked, for the web readout the next slice surfaces.
func TestConvene_MaxConveningsZeroIsUnbounded(t *testing.T) {
	disp := &messageRecordingDispatcher{}
	router, ch := conveningHarness(t, disp, AutonomousConfig{
		Enabled:  true,
		Convener: "nova-sparrow",
		Topic:    "Ad-hoc brainstorm",
		Goal:     "A recommendation.",
		// no schedule, no max_convenings — a one-shot channel.
	})

	for i := 0; i < 4; i++ {
		_, err := router.ConveneChannel(context.Background(), ch)
		require.NoErrorf(t, err, "convening %d is unbounded with max_convenings=0", i+1)
	}
	assert.Equal(t, 4, router.ConveningCount(ch), "the count is tracked even when unbounded")
	assert.Len(t, conveneEnvelopes(disp), 4)
}

// conveningFailingDispatcher records like messageRecordingDispatcher but FAILS
// every convene-lane send — a convener endpoint that is unreachable — so the
// test can prove a missed convening does not consume an aggregate slot.
type conveningFailingDispatcher struct {
	messageRecordingDispatcher
}

func (d *conveningFailingDispatcher) Dispatch(ctx context.Context, env DispatchEnvelope, msg ChannelMessage) error {
	_ = d.messageRecordingDispatcher.Dispatch(ctx, env, msg)
	if env.Convene {
		return errors.New("convener endpoint unreachable")
	}
	return nil
}

// TestConvene_FailedDispatchDoesNotConsumeAConvening — a convene whose opener
// dispatch MISSES must not advance the aggregate count: otherwise a flapping
// convener endpoint would silently exhaust max_convenings without a single
// discussion ever opening. Even attempted more times than the bound, every call
// fails on the DISPATCH error (not the bound error) and the count stays 0.
func TestConvene_FailedDispatchDoesNotConsumeAConvening(t *testing.T) {
	disp := &conveningFailingDispatcher{}
	router, ch := conveningHarness(t, disp, standingArmed(1))

	for i := 0; i < 3; i++ {
		_, err := router.ConveneChannel(context.Background(), ch)
		require.Error(t, err)
		assert.NotErrorIs(t, err, ErrAutonomousConveningBoundReached,
			"a missed convening must not lock the channel out on the aggregate bound")
	}
	assert.Equal(t, 0, router.ConveningCount(ch),
		"a missed opener releases its reserved slot")
}

// TestConveningCount_ZeroForUnconvenedChannel — the accessor reports 0 for a
// channel that has never been convened (the web readout's baseline).
func TestConveningCount_ZeroForUnconvenedChannel(t *testing.T) {
	router, ch := conveningHarness(t, &messageRecordingDispatcher{}, standingArmed(5))
	assert.Equal(t, 0, router.ConveningCount(ch))
}

// countingConveneDispatcher is a thread-safe stand-in for
// messageRecordingDispatcher (whose slice appends are unsynchronized) used by the
// concurrency test: it counts convene-lane sends atomically and never fails.
type countingConveneDispatcher struct{ convenes int32 }

func (d *countingConveneDispatcher) Dispatch(_ context.Context, env DispatchEnvelope, _ ChannelMessage) error {
	if env.Convene {
		atomic.AddInt32(&d.convenes, 1)
	}
	return nil
}

// TestConvene_ConcurrentConvenesRespectBound — the count's half of the idle
// convene race (convene.go's header defers it to "PR 7"): N goroutines convene a
// channel bounded at max_convenings=K < N simultaneously; the atomic
// check-and-reserve admits EXACTLY K and no more, so two convenes at the ceiling
// cannot both slip past a plain read. Run under -race, it also proves the count
// map is accessed only under conveningMu.
func TestConvene_ConcurrentConvenesRespectBound(t *testing.T) {
	const goroutines, bound = 8, 3
	disp := &countingConveneDispatcher{}
	router, ch := conveningHarness(t, disp, standingArmed(bound))

	var wg sync.WaitGroup
	var admitted int32
	for i := 0; i < goroutines; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if _, err := router.ConveneChannel(context.Background(), ch); err == nil {
				atomic.AddInt32(&admitted, 1)
			}
		}()
	}
	wg.Wait()

	assert.Equal(t, int32(bound), admitted, "exactly max_convenings convenings are admitted")
	assert.Equal(t, bound, router.ConveningCount(ch), "the count never overshoots the bound")
	assert.Equal(t, int32(bound), atomic.LoadInt32(&disp.convenes), "exactly the admitted convenings dispatch an opener")
}

// TestPurgeChannelInteraction_ClearsConveningCount — deleting a channel drops
// its convening count with the rest of its resolver state, so the map does not
// leak one entry per deleted standing channel.
func TestPurgeChannelInteraction_ClearsConveningCount(t *testing.T) {
	router, ch := conveningHarness(t, &messageRecordingDispatcher{}, standingArmed(2))

	_, err := router.ConveneChannel(context.Background(), ch)
	require.NoError(t, err)
	require.Equal(t, 1, router.ConveningCount(ch))

	router.PurgeChannelInteraction(ch)
	assert.Equal(t, 0, router.ConveningCount(ch),
		"channel delete clears the convening count")
}
