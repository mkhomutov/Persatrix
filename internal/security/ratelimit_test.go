package security

import (
	"context"
	"fmt"
	"sync"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap/zaptest"
)

// fakeClock is a monotonically advancing clock for deterministic
// sliding-window tests.
type fakeClock struct {
	mu  sync.Mutex
	now time.Time
}

func newFakeClock(start time.Time) *fakeClock { return &fakeClock{now: start} }

func (c *fakeClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.now
}

func (c *fakeClock) Advance(d time.Duration) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.now = c.now.Add(d)
}

// recordingAuditor captures emitted events for assertions. It implements
// the subset of [AuditLogger] the rate limiter touches without requiring
// a file sink.
type recordingAuditor struct {
	mu     sync.Mutex
	events []AuditEvent
}

func newRecordingAuditor() *recordingAuditor { return &recordingAuditor{} }

func (r *recordingAuditor) Emit(_ context.Context, ev AuditEvent) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.events = append(r.events, ev)
	return nil
}

func (r *recordingAuditor) Flush() error { return nil }
func (r *recordingAuditor) Close() error { return nil }
func (r *recordingAuditor) Path() string { return "" }

func (r *recordingAuditor) countByType(t AuditEventType) int {
	r.mu.Lock()
	defer r.mu.Unlock()
	n := 0
	for _, ev := range r.events {
		if ev.EventType == t {
			n++
		}
	}
	return n
}

func newTestRateLimiter(t *testing.T, clock *fakeClock, opts ...RateLimiterOption) *RateLimiter {
	t.Helper()
	cfg := RateLimitConfig{
		CallsPerWindow:    60,
		WindowSeconds:     60,
		MaxTrackedAgents:  1000,
		Logger:            zaptest.NewLogger(t),
		Now:               clock.Now,
		Enabled:           true,
		UnauthenticatedID: "anonymous",
	}
	for _, o := range opts {
		o(&cfg)
	}
	rl, err := NewRateLimiter(cfg)
	require.NoError(t, err)
	return rl
}

func TestSlidingWindow_AllowsUpToLimit(t *testing.T) {
	clk := newFakeClock(time.Unix(0, 0))
	rl := newTestRateLimiter(t, clk)
	for i := 0; i < 60; i++ {
		assert.True(t, rl.Allow("agent-a"), "call %d should be allowed", i)
	}
}

func TestSlidingWindow_DeniesOverLimit(t *testing.T) {
	clk := newFakeClock(time.Unix(0, 0))
	rl := newTestRateLimiter(t, clk)
	for i := 0; i < 60; i++ {
		require.True(t, rl.Allow("agent-a"))
	}
	assert.False(t, rl.Allow("agent-a"), "61st call must be denied")
}

func TestSlidingWindow_RecoversAfterWindow(t *testing.T) {
	clk := newFakeClock(time.Unix(0, 0))
	rl := newTestRateLimiter(t, clk)
	for i := 0; i < 60; i++ {
		require.True(t, rl.Allow("agent-a"))
	}
	require.False(t, rl.Allow("agent-a"))
	clk.Advance(61 * time.Second)
	assert.True(t, rl.Allow("agent-a"), "after window expiry calls must succeed again")
}

func TestSlidingWindow_PerAgentIsolation(t *testing.T) {
	clk := newFakeClock(time.Unix(0, 0))
	rl := newTestRateLimiter(t, clk)
	for i := 0; i < 60; i++ {
		require.True(t, rl.Allow("agent-a"))
	}
	require.False(t, rl.Allow("agent-a"))
	assert.True(t, rl.Allow("agent-b"), "agent-b should be unaffected by agent-a's exhaustion")
}

func TestSlidingWindow_Reset(t *testing.T) {
	clk := newFakeClock(time.Unix(0, 0))
	rl := newTestRateLimiter(t, clk)
	for i := 0; i < 60; i++ {
		require.True(t, rl.Allow("agent-a"))
	}
	require.False(t, rl.Allow("agent-a"))
	rl.Reset("agent-a")
	assert.True(t, rl.Allow("agent-a"), "Reset must clear the window")
}

func TestSlidingWindow_ConcurrentSafe(t *testing.T) {
	clk := newFakeClock(time.Unix(0, 0))
	rl := newTestRateLimiter(t, clk, func(c *RateLimitConfig) { c.CallsPerWindow = 10000 })
	var wg sync.WaitGroup
	for g := 0; g < 100; g++ {
		wg.Add(1)
		agentID := fmt.Sprintf("agent-%d", g%10)
		go func(id string) {
			defer wg.Done()
			for i := 0; i < 100; i++ {
				rl.Allow(id)
			}
		}(agentID)
	}
	wg.Wait()
}

func TestSlidingWindow_LRUEvictionUnderHighCardinality(t *testing.T) {
	clk := newFakeClock(time.Unix(0, 0))
	auditor := newRecordingAuditor()
	rl := newTestRateLimiter(t, clk, func(c *RateLimitConfig) {
		c.MaxTrackedAgents = 8
		c.Auditor = auditor
	})
	// Issue one call from 8 + 5 distinct agents.
	for i := 0; i < 13; i++ {
		clk.Advance(time.Millisecond) // distinct LRU stamps
		rl.Allow(fmt.Sprintf("agent-%03d", i))
	}
	assert.Equal(t, 8, rl.TrackedAgents(), "map size must stay at the cap")
	// The first 5 agents should have been evicted in LRU order.
	assert.GreaterOrEqual(t, auditor.countByType(AuditRateLimitAgentEvicted), 5,
		"expected eviction audit events")
}

func TestSlidingWindow_DisabledAlwaysAllows(t *testing.T) {
	clk := newFakeClock(time.Unix(0, 0))
	rl := newTestRateLimiter(t, clk, func(c *RateLimitConfig) { c.Enabled = false })
	for i := 0; i < 1000; i++ {
		require.True(t, rl.Allow("agent-a"))
	}
}

func TestSlidingWindow_UnauthenticatedCallerEmitsAudit(t *testing.T) {
	clk := newFakeClock(time.Unix(0, 0))
	auditor := newRecordingAuditor()
	rl := newTestRateLimiter(t, clk, func(c *RateLimitConfig) { c.Auditor = auditor })
	rl.Allow("") // empty id -> treated as unauthenticated, still rate-limited per anonymous bucket
	assert.GreaterOrEqual(t, auditor.countByType(AuditRateLimitUnauthenticatedCall), 1)
}

// TestSlidingWindow_LastEmitPurgedOnLRUEviction guards PR #244 review
// M-02: when the LRU ring map evicts an agent, the auxiliary `lastEmit`
// throttle map must drop the same key so it cannot grow without bound
// under self-reported X-Agent-ID flooding. Same-package access is used
// to inspect the private map directly — there is no public accessor and
// adding one purely for tests would widen the API surface unnecessarily.
func TestSlidingWindow_LastEmitPurgedOnLRUEviction(t *testing.T) {
	clk := newFakeClock(time.Unix(0, 0))
	rl := newTestRateLimiter(t, clk, func(c *RateLimitConfig) {
		c.MaxTrackedAgents = 4
		c.CallsPerWindow = 1 // first call admitted; second triggers a deny + lastEmit write
	})
	// Force agent-a to be denied so its lastEmit entry is recorded.
	require.True(t, rl.Allow("agent-a"))
	require.False(t, rl.Allow("agent-a"))
	rl.lastEmitMu.Lock()
	_, hadEntry := rl.lastEmit["agent-a"]
	rl.lastEmitMu.Unlock()
	require.True(t, hadEntry, "precondition: agent-a should have a lastEmit entry after a deny")

	// Push the cardinality past MaxTrackedAgents so agent-a is the
	// oldest-touched and gets evicted from the LRU ring map.
	for i := 0; i < 6; i++ {
		clk.Advance(time.Millisecond)
		rl.Allow(fmt.Sprintf("filler-%02d", i))
	}
	assert.LessOrEqual(t, rl.TrackedAgents(), 4, "ring map must respect MaxTrackedAgents")

	rl.lastEmitMu.Lock()
	_, stillThere := rl.lastEmit["agent-a"]
	rl.lastEmitMu.Unlock()
	assert.False(t, stillThere,
		"lastEmit entry for evicted agent-a must be purged alongside the ring (PR #244 M-02)")
}

// TestSlidingWindow_LastEmitPurgedOnReset extends the M-01 follow-up
// from PR #244 review to the Reset() path: explicit operator-driven
// resets must mirror the LRU-eviction cleanup, otherwise a long-running
// orchestrator that periodically resets agents leaks one lastEmit entry
// per reset. Inspects the private map for the same reasons as the
// LRU-eviction test above.
func TestSlidingWindow_LastEmitPurgedOnReset(t *testing.T) {
	clk := newFakeClock(time.Unix(0, 0))
	rl := newTestRateLimiter(t, clk, func(c *RateLimitConfig) {
		c.CallsPerWindow = 1
	})
	require.True(t, rl.Allow("agent-a"))
	require.False(t, rl.Allow("agent-a"))
	rl.lastEmitMu.Lock()
	_, hadEntry := rl.lastEmit["agent-a"]
	rl.lastEmitMu.Unlock()
	require.True(t, hadEntry, "precondition: lastEmit must be set after a deny")

	rl.Reset("agent-a")

	rl.lastEmitMu.Lock()
	_, stillThere := rl.lastEmit["agent-a"]
	rl.lastEmitMu.Unlock()
	assert.False(t, stillThere,
		"Reset must purge the lastEmit entry to avoid unbounded growth (PR #244 M-01 follow-up)")
}
