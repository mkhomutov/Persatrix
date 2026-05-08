package channels

import (
	"context"
	"fmt"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
)

// slowDispatcher delays each Dispatch call by `delay` so fanout-concurrency
// tests can observe the difference between sequential (O(N × delay)) and
// bounded-concurrent (O(ceil(N / channelFanoutMaxConcurrency) × delay))
// timing. It also tracks peak in-flight dispatches so the bound itself is
// pinned by the test, not just the timing speedup (a "spawn N goroutines
// unbounded" regression would still pass a timing-only assertion).
type slowDispatcher struct {
	delay time.Duration

	mu          sync.Mutex
	inFlight    int
	maxInFlight int
	calls       int
}

func (d *slowDispatcher) Dispatch(ctx context.Context, _ DispatchEnvelope, _ ChannelMessage) error {
	d.mu.Lock()
	d.inFlight++
	d.calls++
	if d.inFlight > d.maxInFlight {
		d.maxInFlight = d.inFlight
	}
	d.mu.Unlock()
	defer func() {
		d.mu.Lock()
		d.inFlight--
		d.mu.Unlock()
	}()
	select {
	case <-time.After(d.delay):
	case <-ctx.Done():
	}
	return nil
}

func (d *slowDispatcher) snapshot() (calls, peak int) {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.calls, d.maxInFlight
}

// TestChannelRouter_Publish_FanoutRunsConcurrently pins ISSUE-0014: with the
// PR-4 gRPC dispatcher live, a sequential per-recipient loop would block
// the publish path for O(N × per-recipient-timeout) on a stalled member.
// Bounded-concurrency fanout collapses that to O(ceil(N / limit) × delay).
//
// The test measures wall-clock fanout time against an 8-recipient channel
// where each Dispatch sleeps 100ms. Sequential dispatch would take ≥ 800ms;
// concurrent dispatch (8 < channelFanoutMaxConcurrency) completes in ~100ms.
// The 400ms threshold is a generous 4× safety margin against scheduler
// jitter while still failing loudly on a regression to sequential.
func TestChannelRouter_Publish_FanoutRunsConcurrently(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	disp := &slowDispatcher{delay: 100 * time.Millisecond}
	router := NewChannelRouter(store, disp, zap.NewNop(), nil)
	ctx := context.Background()

	members := make([]string, 0, 9)
	members = append(members, "alice")
	for i := 0; i < 8; i++ {
		members = append(members, fmt.Sprintf("recipient%d", i))
	}
	id := mustCreateGroup(t, store, "planning", members...)

	start := time.Now()
	require.NoError(t, router.Publish(ctx, ChannelMessage{
		ID: uuid.NewString(), ChannelID: id, SenderID: "alice", Content: "hi",
	}, ""))
	elapsed := time.Since(start)

	calls, _ := disp.snapshot()
	assert.Equal(t, 8, calls, "all 8 non-sender members must be dispatched")
	assert.Less(t, elapsed, 400*time.Millisecond,
		"fanout must run concurrently; sequential dispatch of 8 × 100ms would take ≥ 800ms")
}

// TestChannelRouter_Publish_FanoutRespectsConcurrencyBound pins the upper
// half of ISSUE-0014: an unbounded `go r.dispatcher.Dispatch(...)` would
// also satisfy the timing-speedup test above but would spawn one goroutine
// per recipient — pathological on a 1000-member channel. The bound MUST
// be honoured: peak in-flight dispatches stay ≤ channelFanoutMaxConcurrency
// even when the recipient set exceeds it. Pairs with the timing test so
// that "concurrent but bounded" is pinned, not just "concurrent".
func TestChannelRouter_Publish_FanoutRespectsConcurrencyBound(t *testing.T) {
	store := newTestStore(t, SQLiteOptions{})
	disp := &slowDispatcher{delay: 50 * time.Millisecond}
	router := NewChannelRouter(store, disp, zap.NewNop(), nil)
	ctx := context.Background()

	n := channelFanoutMaxConcurrency + 8 // exceed the bound
	members := make([]string, 0, n+1)
	members = append(members, "alice")
	for i := 0; i < n; i++ {
		members = append(members, fmt.Sprintf("recipient%d", i))
	}
	id := mustCreateGroup(t, store, "big", members...)

	require.NoError(t, router.Publish(ctx, ChannelMessage{
		ID: uuid.NewString(), ChannelID: id, SenderID: "alice", Content: "hi",
	}, ""))

	calls, peak := disp.snapshot()
	assert.Equal(t, n, calls, "all members dispatched")
	assert.LessOrEqual(t, peak, channelFanoutMaxConcurrency,
		"peak in-flight dispatches must not exceed the bound")
	// Sanity-check the lower side too: a regression to sequential dispatch
	// would peak at 1, which would also "respect the bound" trivially.
	assert.Greater(t, peak, 1,
		"fanout must actually run concurrently (peak in-flight > 1)")
}
