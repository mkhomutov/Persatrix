package channels

import (
	"context"
	"fmt"
	"sync"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
)

// gateDispatcher is a dispatcher with externally controllable timing for the
// async-publish robustness tests: each Dispatch announces itself on `started`
// (buffered, non-blocking) and then blocks on `release` until the test closes
// it. With `panicOnDispatch` set it panics instead — used to prove a panicking
// dispatch on a detached fanout goroutine is recovered rather than crashing the
// whole process. Distinct from slowDispatcher (time-delay based) because these
// tests need a deterministic "parked mid-fanout" point, not a duration.
type gateDispatcher struct {
	started         chan struct{}
	release         chan struct{}
	panicOnDispatch bool

	mu    sync.Mutex
	calls int
}

func (d *gateDispatcher) Dispatch(_ context.Context, _ DispatchEnvelope, _ ChannelMessage) error {
	d.mu.Lock()
	d.calls++
	d.mu.Unlock()
	if d.started != nil {
		select {
		case d.started <- struct{}{}:
		default:
		}
	}
	if d.panicOnDispatch {
		panic("gateDispatcher: simulated dispatch panic")
	}
	if d.release != nil {
		<-d.release
	}
	return nil
}

func (d *gateDispatcher) callCount() int {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.calls
}

// TestPublishAsync_FanoutPanic_DoesNotCrashProcess pins the fault-isolation fix:
// the synchronous Publish ran fanout under the server's recoveryMiddleware, but
// PublishAsync detaches fanout — and the per-recipient dispatch workers run on
// their own goroutines on BOTH paths. An unrecovered panic in any goroutine
// terminates the entire orchestrator, so a single dispatcher panic must be
// recovered (logged) rather than taking the process down. If the recover is
// absent the panic propagates and `go test` aborts with a crash — reaching the
// post-drain assertions at all is the proof the fix holds.
func TestPublishAsync_FanoutPanic_DoesNotCrashProcess(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	disp := &gateDispatcher{panicOnDispatch: true}
	router := NewChannelRouter(store, disp, zap.NewNop(), nil)
	ctx := context.Background()

	id := mustCreateGroup(t, store, "planning", "alice", "bob", "carol")

	// Commit succeeds synchronously; the panic happens only in the detached
	// fanout's dispatch worker.
	require.NoError(t, router.PublishAsync(ctx, ChannelMessage{
		ID:        "msg-panic-1",
		ChannelID: id,
		SenderID:  "alice",
		Content:   "boom",
		Timestamp: time.Now().UTC(),
	}, ""))

	// Drains without crashing: the recover ran and released the WaitGroup.
	router.WaitForPendingFanout()

	stored, err := store.GetMessage(ctx, "msg-panic-1")
	require.NoError(t, err)
	assert.Equal(t, "boom", stored.Content, "publish must commit even though fanout panicked")
	assert.GreaterOrEqual(t, disp.callCount(), 1, "fanout must have attempted at least one dispatch")
}

// TestDrainPendingFanout_BoundedByContext pins the shutdown-hang fix: a fanout
// blocked on a slow/silent agent (under floor control, up to M×turnTimeout) must
// not be able to hang process exit. DrainPendingFanout takes a context and
// returns false once it expires rather than blocking forever on the WaitGroup —
// so the orchestrator's bounded shutdown budget is honoured even when a round is
// wedged. A regression to the unbounded `WaitForPendingFanout()` would block
// here until the test deadline.
func TestDrainPendingFanout_BoundedByContext(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	disp := &gateDispatcher{release: make(chan struct{})}
	router := NewChannelRouter(store, disp, zap.NewNop(), nil)
	ctx := context.Background()

	id := mustCreateGroup(t, store, "planning", "alice", "bob", "carol")
	require.NoError(t, router.PublishAsync(ctx, ChannelMessage{
		ID:        "msg-drain-1",
		ChannelID: id,
		SenderID:  "alice",
		Content:   "blocked",
		Timestamp: time.Now().UTC(),
	}, ""))

	// The dispatch is parked on `release`; a bounded drain must give up.
	drainCtx, cancel := context.WithTimeout(ctx, 100*time.Millisecond)
	defer cancel()
	start := time.Now()
	drained := router.DrainPendingFanout(drainCtx)
	elapsed := time.Since(start)
	assert.False(t, drained, "drain must report it did not complete before the deadline")
	assert.Less(t, elapsed, time.Second, "bounded drain must return promptly, not block on the wedged fanout")

	// Release and fully drain so the goroutine cannot outlive the test.
	close(disp.release)
	router.WaitForPendingFanout()
}

// TestPublishAsync_InFlightCap_FallsBackToSync pins the backpressure fix: the
// async seam decoupled publish from fanout, removing the natural backpressure
// the blocking POST used to provide. Without a ceiling a looping client could
// spawn unbounded detached fanout goroutines. PublishAsync caps concurrent
// detached fanouts and, at the ceiling, runs fanout inline (the pre-async
// behaviour) so the (cap+1)th caller pays the latency instead of leaking a
// goroutine — load is shed, never dropped.
func TestPublishAsync_InFlightCap_FallsBackToSync(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	disp := &gateDispatcher{started: make(chan struct{}, 16), release: make(chan struct{})}
	router := NewChannelRouter(store, disp, zap.NewNop(), nil)
	router.SetMaxInFlightFanout(2)
	ctx := context.Background()

	id := mustCreateGroup(t, store, "planning", "alice", "bob")

	// Fill the cap: two async publishes whose single dispatch parks on release.
	for i := 0; i < 2; i++ {
		require.NoError(t, router.PublishAsync(ctx, ChannelMessage{
			ID:        fmt.Sprintf("msg-cap-async-%d", i),
			ChannelID: id,
			SenderID:  "alice",
			Content:   "fill",
			Timestamp: time.Now().UTC(),
		}, ""))
	}
	// Wait until both detached fanouts are actually in flight (parked on
	// dispatch) so the counter has settled at the cap.
	require.Eventually(t, func() bool { return router.inFlightFanout() == 2 },
		time.Second, 5*time.Millisecond, "both async fanouts should be in flight")

	// The 3rd publish is over the cap: it must run fanout inline (blocking on
	// the gated dispatch) rather than spawning a 3rd goroutine. Fire it on its
	// own goroutine so the test can observe that the in-flight count never
	// exceeds the cap.
	done := make(chan error, 1)
	go func() {
		done <- router.PublishAsync(ctx, ChannelMessage{
			ID:        "msg-cap-sync",
			ChannelID: id,
			SenderID:  "alice",
			Content:   "overflow",
			Timestamp: time.Now().UTC(),
		}, "")
	}()

	// Give the overflow publish time to (synchronously) reach dispatch, then
	// assert the detached in-flight count never crept past the cap.
	require.Eventually(t, func() bool { return disp.callCount() >= 3 },
		time.Second, 5*time.Millisecond, "the inline overflow fanout should reach dispatch")
	assert.Equal(t, int64(2), router.inFlightFanout(),
		"the over-cap publish must run inline, not spawn a 3rd detached fanout")

	// Release everything and confirm the inline publish returned and all drains.
	close(disp.release)
	select {
	case err := <-done:
		require.NoError(t, err)
	case <-time.After(2 * time.Second):
		t.Fatal("over-cap inline publish did not return after release")
	}
	router.WaitForPendingFanout()
}

// TestPublishAsync_ReturnsBeforeFanoutCompletes pins the RFC 0048 console
// publish-latency fix: the REST publish path must return as soon as the
// message is PERSISTED, not after the (potentially minutes-long) agent fanout
// completes. The synchronous [ChannelRouter.Publish] blocks on fanout —
// which, with floor control on, serializes per-speaker turns each waiting up
// to the 45s turn timeout — so a human keystroke was coupled to a multi-turn
// LLM round (observed 90-135s POST latencies). [ChannelRouter.PublishAsync]
// detaches the fanout onto a tracked goroutine and returns at the commit.
//
// The slowDispatcher (shared with fanout_test.go) sleeps per Dispatch, so a
// blocking publish would take ≥ delay; PublishAsync must return well under it.
func TestPublishAsync_ReturnsBeforeFanoutCompletes(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	const dispatchDelay = 300 * time.Millisecond
	disp := &slowDispatcher{delay: dispatchDelay}
	router := NewChannelRouter(store, disp, zap.NewNop(), nil)
	ctx := context.Background()

	id := mustCreateGroup(t, store, "planning", "alice", "bob", "carol")

	msg := ChannelMessage{
		ID:        "msg-async-1",
		ChannelID: id,
		SenderID:  "alice",
		Content:   "hello team",
		Timestamp: time.Now().UTC(),
	}

	start := time.Now()
	require.NoError(t, router.PublishAsync(ctx, msg, ""))
	elapsed := time.Since(start)

	// The publish returns at the persistence boundary, not after fanout. A
	// generous bound (half the single-dispatch delay) fails loudly on a
	// regression to synchronous fanout while tolerating scheduler jitter.
	assert.Less(t, elapsed, dispatchDelay/2,
		"PublishAsync must return before the slow fanout completes")

	// The message is durably persisted by the time PublishAsync returns, so the
	// handler's post-publish GET (and the console echo) sees it immediately.
	stored, err := store.GetMessage(ctx, msg.ID)
	require.NoError(t, err)
	assert.Equal(t, "hello team", stored.Content)

	// Fanout still happens — just detached. Drain it and assert both non-sender
	// recipients were dispatched to.
	router.WaitForPendingFanout()
	calls, _ := disp.snapshot()
	assert.Equal(t, 2, calls, "fanout must still reach both non-sender recipients")
}

// TestPublishAsync_PersistFailure_NoFanout pins that a rejected publish (here:
// an unknown channel, which the store refuses) surfaces the error synchronously
// and never spawns a fanout goroutine — the async seam moves only the fanout
// off the request path, not the commit's success/failure contract.
func TestPublishAsync_PersistFailure_NoFanout(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	disp := &slowDispatcher{delay: 10 * time.Millisecond}
	router := NewChannelRouter(store, disp, zap.NewNop(), nil)
	ctx := context.Background()

	err := router.PublishAsync(ctx, ChannelMessage{
		ID:        "msg-async-2",
		ChannelID: "group:does-not-exist",
		SenderID:  "alice",
		Content:   "into the void",
		Timestamp: time.Now().UTC(),
	}, "")
	require.Error(t, err, "publish to a non-existent channel must fail synchronously")

	router.WaitForPendingFanout()
	calls, _ := disp.snapshot()
	assert.Equal(t, 0, calls, "a rejected publish must not dispatch")
}

// TestPublishAsync_MatchesPublishFanout cross-checks that PublishAsync produces
// the same recipient fanout as the synchronous Publish for a representative
// multi-recipient channel — the two entry points must differ only in WHEN the
// HTTP response is written, never in WHO receives the message.
func TestPublishAsync_MatchesPublishFanout(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	disp := &slowDispatcher{delay: time.Millisecond}
	router := NewChannelRouter(store, disp, zap.NewNop(), nil)
	ctx := context.Background()

	id := mustCreateGroup(t, store, "planning", "alice", "bob", "carol", "dave")
	for i := 0; i < 3; i++ {
		require.NoError(t, router.PublishAsync(ctx, ChannelMessage{
			ID:        fmt.Sprintf("msg-async-match-%d", i),
			ChannelID: id,
			SenderID:  "alice",
			Content:   "ping",
			Timestamp: time.Now().UTC(),
		}, ""))
	}
	router.WaitForPendingFanout()

	calls, _ := disp.snapshot()
	// 3 non-sender recipients (bob, carol, dave) × 3 publishes.
	assert.Equal(t, 9, calls)
}
