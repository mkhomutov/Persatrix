package security

import (
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap/zaptest"
)

func newTestBreaker(t *testing.T, clk *fakeClock, opts ...func(*CircuitBreakerConfig)) (*CircuitBreaker, *recordingAuditor) {
	t.Helper()
	auditor := newRecordingAuditor()
	cfg := CircuitBreakerConfig{
		Logger:  zaptest.NewLogger(t),
		Now:     clk.Now,
		Auditor: auditor,
		Thresholds: map[ViolationType]ThresholdRule{
			ViolationCapability: {Count: 3, Window: 5 * time.Minute},
			ViolationRateLimit:  {Count: 5, Window: 10 * time.Minute},
		},
	}
	for _, o := range opts {
		o(&cfg)
	}
	cb, err := NewCircuitBreaker(cfg)
	require.NoError(t, err)
	return cb, auditor
}

func TestCircuitBreaker_OpensAtCapabilityThreshold(t *testing.T) {
	clk := newFakeClock(time.Unix(0, 0))
	cb, _ := newTestBreaker(t, clk)
	for i := 0; i < 3; i++ {
		cb.RecordViolation("agent-a", ViolationCapability)
	}
	assert.True(t, cb.IsQuarantined("agent-a"))
}

func TestCircuitBreaker_DoesNotOpenAcrossWindow(t *testing.T) {
	clk := newFakeClock(time.Unix(0, 0))
	cb, _ := newTestBreaker(t, clk)
	cb.RecordViolation("agent-a", ViolationCapability)
	cb.RecordViolation("agent-a", ViolationCapability)
	clk.Advance(6 * time.Minute)
	cb.RecordViolation("agent-a", ViolationCapability)
	assert.False(t, cb.IsQuarantined("agent-a"))
}

func TestCircuitBreaker_OpenEmitsQuarantineEvent(t *testing.T) {
	clk := newFakeClock(time.Unix(0, 0))
	cb, auditor := newTestBreaker(t, clk)
	for i := 0; i < 3; i++ {
		cb.RecordViolation("agent-a", ViolationCapability)
	}
	assert.Equal(t, 1, auditor.countByType(AuditAgentQuarantined),
		"exactly one quarantine event on first open")
}

func TestCircuitBreaker_PerViolationTypeThresholds(t *testing.T) {
	clk := newFakeClock(time.Unix(0, 0))
	cb, _ := newTestBreaker(t, clk)
	// Capability threshold = 3
	cb.RecordViolation("a", ViolationCapability)
	cb.RecordViolation("a", ViolationCapability)
	require.False(t, cb.IsQuarantined("a"))
	cb.RecordViolation("a", ViolationCapability)
	require.True(t, cb.IsQuarantined("a"))

	// Rate-limit threshold = 5 — separate agent so prior open does not bleed.
	for i := 0; i < 4; i++ {
		cb.RecordViolation("b", ViolationRateLimit)
	}
	require.False(t, cb.IsQuarantined("b"))
	cb.RecordViolation("b", ViolationRateLimit)
	require.True(t, cb.IsQuarantined("b"))
}

func TestCircuitBreaker_Unquarantine(t *testing.T) {
	clk := newFakeClock(time.Unix(0, 0))
	cb, auditor := newTestBreaker(t, clk)
	for i := 0; i < 3; i++ {
		cb.RecordViolation("a", ViolationCapability)
	}
	require.True(t, cb.IsQuarantined("a"))
	require.True(t, cb.Unquarantine("a", "operator-test"))
	assert.False(t, cb.IsQuarantined("a"))
	assert.Equal(t, 1, auditor.countByType(AuditAgentUnquarantined))
	// Counters cleared so the agent does not re-quarantine on the next
	// violation alone.
	cb.RecordViolation("a", ViolationCapability)
	assert.False(t, cb.IsQuarantined("a"))
}

// TestCircuitBreaker_ViolationsClearedOnQuarantine guards PR #244 review
// M-03 (partial): when the breaker opens, the per-agent entry in the
// `violations` map must be removed so the historical timestamps do not
// linger across the agent's lifetime. Without this, a long-running
// orchestrator accumulates one stale entry per ever-quarantined agent.
// Same-package access is used to inspect the private map.
func TestCircuitBreaker_ViolationsClearedOnQuarantine(t *testing.T) {
	clk := newFakeClock(time.Unix(0, 0))
	cb, _ := newTestBreaker(t, clk)
	for i := 0; i < 3; i++ {
		cb.RecordViolation("a", ViolationCapability)
	}
	require.True(t, cb.IsQuarantined("a"))
	cb.mu.Lock()
	_, present := cb.violations["a"]
	cb.mu.Unlock()
	assert.False(t, present,
		"violations entry for quarantined agent must be cleared (PR #244 M-03)")
}

// TestCircuitBreaker_HasAnyQuarantined_AtomicCountTracksMap pins the
// PR #244 round-2 review L-05 invariant: `quarantinedCount` (the
// lock-free atomic backing HasAnyQuarantined on the request hot path)
// must stay in sync with len(quarantined) across open→close→open
// cycles. A drift here would either leak the H-01 anonymous-deny after
// the operator clears the quarantine (false positive — denies all
// anonymous traffic forever) or fail to engage it after a re-open
// (false negative — defeats the H-01 fix).
func TestCircuitBreaker_HasAnyQuarantined_AtomicCountTracksMap(t *testing.T) {
	clk := newFakeClock(time.Unix(0, 0))
	cb, _ := newTestBreaker(t, clk)

	require.False(t, cb.HasAnyQuarantined(), "initial state: no quarantines")
	assert.Equal(t, int32(0), cb.quarantinedCount.Load())

	// Open agent-a.
	for i := 0; i < 3; i++ {
		cb.RecordViolation("a", ViolationCapability)
	}
	require.True(t, cb.HasAnyQuarantined())
	assert.Equal(t, int32(1), cb.quarantinedCount.Load())

	// Open agent-b — count must reflect both.
	for i := 0; i < 3; i++ {
		cb.RecordViolation("b", ViolationCapability)
	}
	assert.Equal(t, int32(2), cb.quarantinedCount.Load())

	// Release agent-a — count drops to 1, HasAnyQuarantined still true.
	require.True(t, cb.Unquarantine("a", "operator-test"))
	assert.True(t, cb.HasAnyQuarantined())
	assert.Equal(t, int32(1), cb.quarantinedCount.Load())

	// Release agent-b — count drops to 0, HasAnyQuarantined flips false.
	require.True(t, cb.Unquarantine("b", "operator-test"))
	assert.False(t, cb.HasAnyQuarantined(),
		"L-05: anonymous-deny must release immediately when last quarantine clears")
	assert.Equal(t, int32(0), cb.quarantinedCount.Load())

	// Re-open agent-a after release — counter must re-engage (guards
	// against an Unquarantine-then-RecordViolation off-by-one).
	for i := 0; i < 3; i++ {
		cb.RecordViolation("a", ViolationCapability)
	}
	assert.True(t, cb.HasAnyQuarantined())
	assert.Equal(t, int32(1), cb.quarantinedCount.Load())

	// Idempotent unquarantine (no-op branch) must not touch the counter.
	assert.False(t, cb.Unquarantine("never-existed", "operator-test"))
	assert.Equal(t, int32(1), cb.quarantinedCount.Load(),
		"no-op Unquarantine must not decrement the counter")
}

// TestCircuitBreaker_RejectsZeroWindow guards ISSUE-0001: a zero-duration
// Window on a non-Disabled rule silently neutralises the breaker because
// `now.Add(-0) == now` drops every prior entry from the rolling counter.
// `NewCircuitBreaker` must reject the bad config rather than boot a
// breaker that can never open for that violation type.
func TestCircuitBreaker_RejectsZeroWindow(t *testing.T) {
	_, err := NewCircuitBreaker(CircuitBreakerConfig{
		Logger: zaptest.NewLogger(t),
		Thresholds: map[ViolationType]ThresholdRule{
			ViolationCapability: {Count: 3, Window: 0},
		},
	})
	require.Error(t, err)
	assert.Contains(t, err.Error(), "Window")
	assert.Contains(t, err.Error(), "capability")
}

// TestCircuitBreaker_RejectsNegativeWindow mirrors the zero-window case;
// negative durations would also silently neutralise the breaker.
func TestCircuitBreaker_RejectsNegativeWindow(t *testing.T) {
	_, err := NewCircuitBreaker(CircuitBreakerConfig{
		Logger: zaptest.NewLogger(t),
		Thresholds: map[ViolationType]ThresholdRule{
			ViolationRateLimit: {Count: 5, Window: -time.Second},
		},
	})
	require.Error(t, err)
}

// TestCircuitBreaker_RejectsZeroCount guards the second half of the
// invariant: Count <= 0 means `len(kept) >= rule.Count` is satisfied
// before any violation is recorded, opening the breaker on first call.
// Treat as a config error.
func TestCircuitBreaker_RejectsZeroCount(t *testing.T) {
	_, err := NewCircuitBreaker(CircuitBreakerConfig{
		Logger: zaptest.NewLogger(t),
		Thresholds: map[ViolationType]ThresholdRule{
			ViolationCapability: {Count: 0, Window: time.Minute},
		},
	})
	require.Error(t, err)
	assert.Contains(t, err.Error(), "Count")
}

// TestCircuitBreaker_DisabledRuleAcceptsZeroWindow keeps the test seam:
// rules marked Disabled bypass the Count/Window validators (their fields
// are unused on the disabled path). Tests that previously relied on the
// implicit `Window: 0 → never open` behaviour migrate to `Disabled: true`.
func TestCircuitBreaker_DisabledRuleAcceptsZeroWindow(t *testing.T) {
	cb, err := NewCircuitBreaker(CircuitBreakerConfig{
		Logger: zaptest.NewLogger(t),
		Thresholds: map[ViolationType]ThresholdRule{
			ViolationCapability: {Disabled: true},
		},
	})
	require.NoError(t, err)
	require.NotNil(t, cb)
}

// TestCircuitBreaker_DisabledRuleNeverOpens pins the runtime contract:
// a Disabled rule records nothing toward quarantine — RecordViolation is
// effectively a no-op for that violation type.
func TestCircuitBreaker_DisabledRuleNeverOpens(t *testing.T) {
	clk := newFakeClock(time.Unix(0, 0))
	cb, err := NewCircuitBreaker(CircuitBreakerConfig{
		Logger: zaptest.NewLogger(t),
		Now:    clk.Now,
		Thresholds: map[ViolationType]ThresholdRule{
			ViolationCapability: {Disabled: true},
		},
	})
	require.NoError(t, err)
	for i := 0; i < 100; i++ {
		cb.RecordViolation("a", ViolationCapability)
	}
	assert.False(t, cb.IsQuarantined("a"),
		"Disabled rules must not contribute to quarantine state")
}

// TestCircuitBreaker_RejectsZeroWindowEvenWithCount1 makes the
// migration of test seams explicit: callers that previously used
// `{Count: 1, Window: 0}` to "trip on first violation" must now use a
// finite Window (any positive duration trips on first call when
// Count == 1, since len(kept) becomes 1 on the first record).
func TestCircuitBreaker_RejectsZeroWindowEvenWithCount1(t *testing.T) {
	_, err := NewCircuitBreaker(CircuitBreakerConfig{
		Logger: zaptest.NewLogger(t),
		Thresholds: map[ViolationType]ThresholdRule{
			ViolationCapability: {Count: 1, Window: 0},
		},
	})
	require.Error(t, err)
}
