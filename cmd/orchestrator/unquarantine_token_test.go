package main

import (
	"context"
	"sync"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
	"go.uber.org/zap/zaptest/observer"

	"github.com/mkhomutov/persatrix/internal/security"
)

// PR #244 round-2 review M-05: when SECURITY_UNQUARANTINE_TOKEN is unset
// the unquarantine REST endpoint stays open as a deliberate stop-gap
// (full identity is RFC 0009 Phase 4). That choice inverts the project's
// deny-by-default rule, so it must be recorded explicitly via a startup
// WARN log AND a security-class audit event — silent acceptance of an
// open auth-undo endpoint is the failure mode we are guarding against.
//
// These tests pin both signals plus the Info-log path when the token IS
// configured (no false WARN).

// recordingAuditor is a minimal in-memory AuditLogger used to assert
// emit calls without booting the full audit pipeline. Mirrors the
// pattern used in internal/security tests.
type recordingAuditor struct {
	mu     sync.Mutex
	events []security.AuditEvent
}

func (r *recordingAuditor) Emit(_ context.Context, ev security.AuditEvent) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.events = append(r.events, ev)
	return nil
}

func (r *recordingAuditor) Close() error { return nil }
func (r *recordingAuditor) Flush() error { return nil }
func (r *recordingAuditor) Path() string { return "" }

func (r *recordingAuditor) byType(t security.AuditEventType) []security.AuditEvent {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := []security.AuditEvent{}
	for _, e := range r.events {
		if e.EventType == t {
			out = append(out, e)
		}
	}
	return out
}

func TestUnquarantineToken_UnsetEmitsWarnAndAuditEvent(t *testing.T) {
	t.Setenv("SECURITY_UNQUARANTINE_TOKEN", "")

	core, recorded := observer.New(zap.WarnLevel)
	logger := zap.New(core)
	auditor := &recordingAuditor{}

	tok := unquarantineToken(logger, auditor)
	assert.Equal(t, "", tok, "unset env must yield empty token")

	// WARN log emitted exactly once with the documented message.
	warnEntries := recorded.FilterMessage("security.unquarantine.unauthenticated scope=startup").All()
	require.Len(t, warnEntries, 1,
		"M-05: unset token must produce exactly one WARN log so the open posture is visible in stdout/log aggregation")
	assert.Equal(t, zap.WarnLevel, warnEntries[0].Level)

	// Security-class audit event emitted exactly once.
	events := auditor.byType(security.AuditUnquarantineEndpointOpen)
	require.Len(t, events, 1,
		"M-05: unset token must emit unquarantine.endpoint.open once so the choice lands in the tamper-evident chain")
	assert.Equal(t, "startup", events[0].Action)
	assert.Equal(t, "open", events[0].Outcome)
	assert.Equal(t, "unquarantine_endpoint", events[0].Resource)
	assert.Equal(t, "SECURITY_UNQUARANTINE_TOKEN", events[0].Detail["source"])

	// Confirm the audit event is classified security-class so the
	// fsync path is exercised (telemetry-class would be batched and
	// could be lost on crash — exactly what M-05 is preventing).
	assert.True(t, security.IsSecurityEvent(security.AuditUnquarantineEndpointOpen),
		"unquarantine.endpoint.open must be security-class (per-event fsync)")
}

func TestUnquarantineToken_SetEmitsInfoNotWarn(t *testing.T) {
	t.Setenv("SECURITY_UNQUARANTINE_TOKEN", "s3cret")

	core, recorded := observer.New(zap.InfoLevel)
	logger := zap.New(core)
	auditor := &recordingAuditor{}

	tok := unquarantineToken(logger, auditor)
	assert.Equal(t, "s3cret", tok)

	// Info-level confirmation logged.
	infoEntries := recorded.FilterMessage("unquarantine endpoint protected by shared secret").All()
	assert.Len(t, infoEntries, 1)

	// No WARN, no audit event — the open-posture signals must not fire
	// when the token IS configured.
	assert.Empty(t, recorded.FilterMessage("security.unquarantine.unauthenticated scope=startup").All(),
		"WARN must not fire when token is configured")
	assert.Empty(t, auditor.byType(security.AuditUnquarantineEndpointOpen),
		"audit event must not fire when token is configured")
}

func TestUnquarantineToken_NilAuditorIsSafe(t *testing.T) {
	// emitUnquarantineEndpointOpen must be nil-safe so the orchestrator
	// boots cleanly when audit is disabled (OBSERVABILITY_AUDIT_PATH=off).
	t.Setenv("SECURITY_UNQUARANTINE_TOKEN", "")
	logger := zap.NewNop()
	tok := unquarantineToken(logger, nil)
	assert.Equal(t, "", tok)
}
