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
//
// ISSUE-0007: contexts are captured alongside events so tests can assert
// the request ctx (and any trace metadata it carries) reaches the audit
// sink rather than the previous `context.Background()` it received.
type recordingAuditor struct {
	mu     sync.Mutex
	events []AuditEvent
	ctxs   []context.Context
}

func newRecordingAuditor() *recordingAuditor { return &recordingAuditor{} }

func (r *recordingAuditor) Emit(ctx context.Context, ev AuditEvent) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.events = append(r.events, ev)
	r.ctxs = append(r.ctxs, ctx)
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

// snapshotByType returns a copy of the (event, ctx) pairs whose event
// type matches t. Snapshot under lock so callers can iterate without
// holding the auditor mutex.
func (r *recordingAuditor) snapshotByType(t AuditEventType) []recordedEmit {
	r.mu.Lock()
	defer r.mu.Unlock()
	var out []recordedEmit
	for i, ev := range r.events {
		if ev.EventType == t {
			out = append(out, recordedEmit{event: ev, ctx: r.ctxs[i]})
		}
	}
	return out
}

type recordedEmit struct {
	event AuditEvent
	ctx   context.Context
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
	ctx := context.Background()
	for i := 0; i < 60; i++ {
		assert.True(t, rl.Allow(ctx, "agent-a"), "call %d should be allowed", i)
	}
}

func TestSlidingWindow_DeniesOverLimit(t *testing.T) {
	clk := newFakeClock(time.Unix(0, 0))
	rl := newTestRateLimiter(t, clk)
	ctx := context.Background()
	for i := 0; i < 60; i++ {
		require.True(t, rl.Allow(ctx, "agent-a"))
	}
	assert.False(t, rl.Allow(ctx, "agent-a"), "61st call must be denied")
}

func TestSlidingWindow_RecoversAfterWindow(t *testing.T) {
	clk := newFakeClock(time.Unix(0, 0))
	rl := newTestRateLimiter(t, clk)
	ctx := context.Background()
	for i := 0; i < 60; i++ {
		require.True(t, rl.Allow(ctx, "agent-a"))
	}
	require.False(t, rl.Allow(ctx, "agent-a"))
	clk.Advance(61 * time.Second)
	assert.True(t, rl.Allow(ctx, "agent-a"), "after window expiry calls must succeed again")
}

func TestSlidingWindow_PerAgentIsolation(t *testing.T) {
	clk := newFakeClock(time.Unix(0, 0))
	rl := newTestRateLimiter(t, clk)
	ctx := context.Background()
	for i := 0; i < 60; i++ {
		require.True(t, rl.Allow(ctx, "agent-a"))
	}
	require.False(t, rl.Allow(ctx, "agent-a"))
	assert.True(t, rl.Allow(ctx, "agent-b"), "agent-b should be unaffected by agent-a's exhaustion")
}

func TestSlidingWindow_Reset(t *testing.T) {
	clk := newFakeClock(time.Unix(0, 0))
	rl := newTestRateLimiter(t, clk)
	ctx := context.Background()
	for i := 0; i < 60; i++ {
		require.True(t, rl.Allow(ctx, "agent-a"))
	}
	require.False(t, rl.Allow(ctx, "agent-a"))
	rl.Reset(ctx, "agent-a", "test")
	assert.True(t, rl.Allow(ctx, "agent-a"), "Reset must clear the window")
}

func TestSlidingWindow_ConcurrentSafe(t *testing.T) {
	clk := newFakeClock(time.Unix(0, 0))
	rl := newTestRateLimiter(t, clk, func(c *RateLimitConfig) { c.CallsPerWindow = 10000 })
	ctx := context.Background()
	var wg sync.WaitGroup
	for g := 0; g < 100; g++ {
		wg.Add(1)
		agentID := fmt.Sprintf("agent-%d", g%10)
		go func(id string) {
			defer wg.Done()
			for i := 0; i < 100; i++ {
				rl.Allow(ctx, id)
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
	ctx := context.Background()
	// Issue one call from 8 + 5 distinct agents.
	for i := 0; i < 13; i++ {
		clk.Advance(time.Millisecond) // distinct LRU stamps
		rl.Allow(ctx, fmt.Sprintf("agent-%03d", i))
	}
	assert.Equal(t, 8, rl.TrackedAgents(), "map size must stay at the cap")
	// The first 5 agents should have been evicted in LRU order.
	assert.GreaterOrEqual(t, auditor.countByType(AuditRateLimitAgentEvicted), 5,
		"expected eviction audit events")
}

func TestSlidingWindow_DisabledAlwaysAllows(t *testing.T) {
	clk := newFakeClock(time.Unix(0, 0))
	rl := newTestRateLimiter(t, clk, func(c *RateLimitConfig) { c.Enabled = false })
	ctx := context.Background()
	for i := 0; i < 1000; i++ {
		require.True(t, rl.Allow(ctx, "agent-a"))
	}
}

func TestSlidingWindow_UnauthenticatedCallerEmitsAudit(t *testing.T) {
	clk := newFakeClock(time.Unix(0, 0))
	auditor := newRecordingAuditor()
	rl := newTestRateLimiter(t, clk, func(c *RateLimitConfig) { c.Auditor = auditor })
	rl.Allow(context.Background(), "") // empty id -> treated as unauthenticated, still rate-limited per anonymous bucket
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
	ctx := context.Background()
	// Force agent-a to be denied so its lastEmit entry is recorded.
	require.True(t, rl.Allow(ctx, "agent-a"))
	require.False(t, rl.Allow(ctx, "agent-a"))
	rl.lastEmitMu.Lock()
	_, hadEntry := rl.lastEmit["agent-a"]
	rl.lastEmitMu.Unlock()
	require.True(t, hadEntry, "precondition: agent-a should have a lastEmit entry after a deny")

	// Push the cardinality past MaxTrackedAgents so agent-a is the
	// oldest-touched and gets evicted from the LRU ring map.
	for i := 0; i < 6; i++ {
		clk.Advance(time.Millisecond)
		rl.Allow(ctx, fmt.Sprintf("filler-%02d", i))
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
	ctx := context.Background()
	require.True(t, rl.Allow(ctx, "agent-a"))
	require.False(t, rl.Allow(ctx, "agent-a"))
	rl.lastEmitMu.Lock()
	_, hadEntry := rl.lastEmit["agent-a"]
	rl.lastEmitMu.Unlock()
	require.True(t, hadEntry, "precondition: lastEmit must be set after a deny")

	rl.Reset(ctx, "agent-a", "test")

	rl.lastEmitMu.Lock()
	_, stillThere := rl.lastEmit["agent-a"]
	rl.lastEmitMu.Unlock()
	assert.False(t, stillThere,
		"Reset must purge the lastEmit entry to avoid unbounded growth (PR #244 M-01 follow-up)")
}

// rateLimitCtxKey is a private key type so the test's marker value
// cannot collide with anything the limiter or auditor stamps on the
// context internally.
type rateLimitCtxKey struct{}

// TestRateLimiter_AllowPropagatesRequestCtxToAuditor pins ISSUE-0007:
// audit emits triggered from Allow (`rate_limit.unauthenticated_caller`,
// `rate_limit.violated`, `rate_limit.evict`) must carry the caller's
// request context so trace IDs survive into the audit chain. Prior to
// the fix, `emit` handed `context.Background()` to the auditor and the
// trace correlation broke at the security boundary.
func TestRateLimiter_AllowPropagatesRequestCtxToAuditor(t *testing.T) {
	clk := newFakeClock(time.Unix(0, 0))
	auditor := newRecordingAuditor()
	rl := newTestRateLimiter(t, clk, func(c *RateLimitConfig) {
		c.CallsPerWindow = 1
		c.Auditor = auditor
	})

	ctx := context.WithValue(context.Background(), rateLimitCtxKey{}, "trace-abc")

	// First call admitted but anonymous → emits unauthenticated_caller.
	require.True(t, rl.Allow(ctx, ""))
	// Second call denied → emits violation.
	require.False(t, rl.Allow(ctx, ""))

	for _, et := range []AuditEventType{AuditRateLimitUnauthenticatedCall, AuditRateLimitViolated} {
		emits := auditor.snapshotByType(et)
		require.NotEmpty(t, emits, "expected at least one %s emit", et)
		for _, e := range emits {
			require.NotNil(t, e.ctx, "%s emit must receive a non-nil ctx", et)
			got, _ := e.ctx.Value(rateLimitCtxKey{}).(string)
			assert.Equal(t, "trace-abc", got,
				"ISSUE-0007: %s emit must propagate the caller's request ctx (got value %q)", et, got)
		}
	}
}

// TestRateLimiter_AuditEmitNotCancelledWithRequestCtx pins the
// "use context.WithoutCancel" half of ISSUE-0007: the auditor handoff
// must not be cancelled when the inbound request context is, otherwise
// a fast client cancel would lose the very deny event the limiter just
// fired. Mirrors `Server.emitAudit` (`internal/server/server.go`)
// which uses `context.WithoutCancel(r.Context())` for the same reason.
func TestRateLimiter_AuditEmitNotCancelledWithRequestCtx(t *testing.T) {
	clk := newFakeClock(time.Unix(0, 0))
	auditor := newRecordingAuditor()
	rl := newTestRateLimiter(t, clk, func(c *RateLimitConfig) {
		c.CallsPerWindow = 1
		c.Auditor = auditor
	})

	parent, cancel := context.WithCancel(context.Background())
	cancel() // request already cancelled by the time we hit the limiter

	require.True(t, rl.Allow(parent, "agent-a"))
	require.False(t, rl.Allow(parent, "agent-a"))

	emits := auditor.snapshotByType(AuditRateLimitViolated)
	require.NotEmpty(t, emits, "expected a rate_limit.violated emit")
	for _, e := range emits {
		assert.NoError(t, e.ctx.Err(),
			"ISSUE-0007: auditor ctx must not inherit cancellation from the request ctx (use context.WithoutCancel)")
	}
}

// TestRateLimiter_AllowSteadyStateZeroAlloc pins ISSUE-0003: under
// sustained admit traffic from a single agent, Allow's hot path must
// not allocate. The previous evictOlderThan implementation built a
// transient `kept []time.Time` slice on every admit, producing GC
// pressure under exactly the flooding load the limiter is meant to
// defend against. The fix compacts the per-agent ring in place, so
// AllocsPerRun for a steady-state Allow must be zero.
//
// Failure mode this guards against: a future contributor "tidying" the
// ring by reintroducing a slice-builder pattern in evictOlderThan would
// not be caught by the correctness tests above.
func TestRateLimiter_AllowSteadyStateZeroAlloc(t *testing.T) {
	clk := newFakeClock(time.Unix(0, 0))
	// CallsPerWindow=600 matches the production-relevant load profile
	// called out in ISSUE-0003. At smaller sizes the Go compiler can
	// stack-allocate the transient slice via escape analysis, hiding
	// the pre-fix regression — so the test must exercise a ring big
	// enough that `make([]time.Time, 0, r.count)` reliably escapes.
	rl := newTestRateLimiter(t, clk, func(c *RateLimitConfig) {
		c.CallsPerWindow = 600
	})
	ctx := context.Background()

	// Warm up: fill the ring and then drift the clock so each subsequent
	// admit forces evictOlderThan to drop expired entries — the exact
	// path that previously allocated.
	for i := 0; i < 600; i++ {
		require.True(t, rl.Allow(ctx, "agent-a"))
	}
	clk.Advance(61 * time.Second)
	for i := 0; i < 600; i++ {
		require.True(t, rl.Allow(ctx, "agent-a"))
	}

	// Under steady state, advance one tick per call and admit. With the
	// ring at capacity and the oldest entry just past the cutoff, every
	// call exercises the eviction-then-append path.
	allocs := testing.AllocsPerRun(200, func() {
		clk.Advance(time.Millisecond)
		rl.Allow(ctx, "agent-a")
	})
	assert.Equal(t, 0.0, allocs,
		"ISSUE-0003: Allow steady-state path must be zero-alloc (got %.2f allocs/op)", allocs)
}

// TestAgentRing_EvictOlderThan_PreservesOrderAndCount pins the
// in-place compaction contract: after evictOlderThan, the ring's live
// entries must still be reachable in chronological order via the
// (head - count) walk, and entries newer than the cutoff must survive
// while entries at or before the cutoff are dropped.
//
// This is the unit-level red test for ISSUE-0003: it lets us verify
// the in-place compaction is correct without going through the full
// Allow path.
func TestAgentRing_EvictOlderThan_PreservesOrderAndCount(t *testing.T) {
	t0 := time.Unix(0, 0)
	r := &agentRing{
		agentID: "agent-a",
		calls:   make([]time.Time, 5),
	}
	// Append 5 timestamps at +1, +2, +3, +4, +5 seconds.
	for i := 1; i <= 5; i++ {
		r.append(t0.Add(time.Duration(i) * time.Second))
	}
	require.Equal(t, 5, r.count)

	// Cutoff = +2.5s — drops entries at +1 and +2; keeps +3, +4, +5.
	r.evictOlderThan(t0.Add(2500 * time.Millisecond))
	assert.Equal(t, 3, r.count, "two oldest entries must be dropped")

	// Walk live entries in chronological order; they must be the three
	// surviving timestamps in ascending order.
	ringCap := len(r.calls)
	start := (r.head - r.count + ringCap) % ringCap
	got := make([]time.Time, 0, r.count)
	for i := 0; i < r.count; i++ {
		got = append(got, r.calls[(start+i)%ringCap])
	}
	want := []time.Time{
		t0.Add(3 * time.Second),
		t0.Add(4 * time.Second),
		t0.Add(5 * time.Second),
	}
	assert.Equal(t, want, got, "survivors must remain in chronological order")

	// A subsequent append must land at the correct slot and bring count
	// back up — proving head/count remain consistent post-eviction.
	r.append(t0.Add(6 * time.Second))
	assert.Equal(t, 4, r.count)
	start = (r.head - r.count + ringCap) % ringCap
	got = got[:0]
	for i := 0; i < r.count; i++ {
		got = append(got, r.calls[(start+i)%ringCap])
	}
	assert.Equal(t, []time.Time{
		t0.Add(3 * time.Second),
		t0.Add(4 * time.Second),
		t0.Add(5 * time.Second),
		t0.Add(6 * time.Second),
	}, got, "post-eviction append must extend the chronological run")
}

// BenchmarkAllowSteadyState gives operators a runnable "what does the
// hot path cost" handle next to the correctness tests. The companion
// regression guard is TestRateLimiter_AllowSteadyStateZeroAlloc above;
// this benchmark is informational and not asserted against in CI.
func BenchmarkAllowSteadyState(b *testing.B) {
	clk := newFakeClock(time.Unix(0, 0))
	cfg := RateLimitConfig{
		CallsPerWindow:    600,
		WindowSeconds:     60,
		MaxTrackedAgents:  1000,
		Enabled:           true,
		UnauthenticatedID: "anonymous",
		Now:               clk.Now,
	}
	rl, err := NewRateLimiter(cfg)
	if err != nil {
		b.Fatal(err)
	}
	ctx := context.Background()
	for i := 0; i < cfg.CallsPerWindow; i++ {
		rl.Allow(ctx, "agent-a")
	}
	clk.Advance(time.Duration(cfg.WindowSeconds+1) * time.Second)
	for i := 0; i < cfg.CallsPerWindow; i++ {
		rl.Allow(ctx, "agent-a")
	}

	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		clk.Advance(time.Millisecond)
		rl.Allow(ctx, "agent-a")
	}
}
