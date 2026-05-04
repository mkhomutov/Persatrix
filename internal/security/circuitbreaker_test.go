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
