package channels

import (
	"context"
	"fmt"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
)

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
