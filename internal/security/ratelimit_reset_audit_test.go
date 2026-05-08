package security

import (
	"context"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestRateLimiter_ResetEmitsAuditEvent pins ISSUE-0005: Reset on a
// tracked agent must emit a `rate_limit.reset` audit event so the
// administrative state mutation lands in the tamper-evident chain —
// mirroring [CircuitBreaker.Unquarantine] which emits
// `agent.unquarantined`. The actor argument is recorded on the event
// for forensics so an operator-driven reset is distinguishable from
// future automated callers.
func TestRateLimiter_ResetEmitsAuditEvent(t *testing.T) {
	clk := newFakeClock(time.Unix(0, 0))
	auditor := newRecordingAuditor()
	rl := newTestRateLimiter(t, clk, func(c *RateLimitConfig) { c.Auditor = auditor })
	ctx := context.Background()
	require.True(t, rl.Allow(ctx, "agent-a"))

	ok := rl.Reset(ctx, "agent-a", "operator-test")
	assert.True(t, ok, "Reset must report true when the agent was tracked")

	emits := auditor.snapshotByType(AuditRateLimitReset)
	require.Len(t, emits, 1, "expected exactly one rate_limit.reset emit")
	ev := emits[0].event
	assert.Equal(t, "agent-a", ev.AgentID)
	assert.Equal(t, "agent-a", ev.Resource)
	assert.Equal(t, "rate_limit.reset", ev.Action)
	assert.Equal(t, "reset", ev.Outcome)
	assert.Equal(t, "operator-test", ev.Detail["actor"],
		"actor must be recorded on the event for forensics")
}

// TestRateLimiter_ResetUnknownAgentNoAudit pins the Unquarantine-mirror
// no-op semantics: Reset on an agent that has never been admitted must
// return false and emit nothing — the ring map was untouched, so an
// audit event would falsely imply a state mutation.
func TestRateLimiter_ResetUnknownAgentNoAudit(t *testing.T) {
	clk := newFakeClock(time.Unix(0, 0))
	auditor := newRecordingAuditor()
	rl := newTestRateLimiter(t, clk, func(c *RateLimitConfig) { c.Auditor = auditor })
	ctx := context.Background()

	ok := rl.Reset(ctx, "never-seen", "operator-test")
	assert.False(t, ok, "Reset on an unknown agent must report false")
	assert.Equal(t, 0, auditor.countByType(AuditRateLimitReset),
		"Reset on an unknown agent must not emit an audit event")
}

// TestRateLimiter_ResetAuditPropagatesRequestCtx mirrors ISSUE-0007 for
// the new Reset emit: the auditor must receive the caller's request
// context so trace IDs survive into the audit chain when an operator
// endpoint eventually drives Reset from a request handler.
func TestRateLimiter_ResetAuditPropagatesRequestCtx(t *testing.T) {
	clk := newFakeClock(time.Unix(0, 0))
	auditor := newRecordingAuditor()
	rl := newTestRateLimiter(t, clk, func(c *RateLimitConfig) { c.Auditor = auditor })
	require.True(t, rl.Allow(context.Background(), "agent-a"))

	ctx := context.WithValue(context.Background(), rateLimitCtxKey{}, "trace-reset")
	require.True(t, rl.Reset(ctx, "agent-a", "operator-test"))

	emits := auditor.snapshotByType(AuditRateLimitReset)
	require.Len(t, emits, 1)
	got, _ := emits[0].ctx.Value(rateLimitCtxKey{}).(string)
	assert.Equal(t, "trace-reset", got,
		"Reset audit emit must carry the caller's request ctx")
}
