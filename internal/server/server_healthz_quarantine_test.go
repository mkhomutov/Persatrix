package server

import (
	"context"
	"net/http"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/mkhomutov/persatrix/internal/security"
)

// PR #244 round-2 review H-03 — `/healthz` regression during quarantine.
//
// The PR #244 H-01 fix denies anonymous (empty `X-Agent-ID`) requests
// while any agent is quarantined. Because the rate-limit middleware was
// originally mounted on the root handler, that deny also caught
// `/healthz` — Kubernetes liveness probes do not (and should not)
// carry `X-Agent-ID`, so a single quarantined agent would crashloop the
// orchestrator (probe → 403 → pod restart → in-memory quarantine state
// lost → traffic resumes → re-quarantine → loop).
//
// These tests pin the fix: `/healthz` must always return 200, even with
// an active quarantine, while `/api/v1/*` anonymous calls remain denied
// (the H-01 protection must not regress).
//
// Companion to [TestRESTMiddleware_QuarantineActiveBlocksAnonymous]
// in the security package, which verifies the deny path against an
// arbitrary handler in isolation.

// TestHealthzAllowedDuringQuarantine asserts the H-03 fix: probes pass
// through untouched even when an agent is quarantined.
func TestHealthzAllowedDuringQuarantine(t *testing.T) {
	cb := breakerWithThreshold(t)
	srv := testServerWithBreaker(t, cb)

	// Seed quarantine state so the middleware would otherwise deny
	// anonymous traffic via the H-01 protection.
	cb.RecordViolation(context.Background(), "agent-bad", security.ViolationCapability)
	require.True(t, cb.IsQuarantined("agent-bad"),
		"precondition: agent-bad must be quarantined to exercise H-03")

	rec := doRequest(srv.Handler(), http.MethodGet, "/healthz", nil)
	assert.Equal(t, http.StatusOK, rec.Code,
		"H-03: /healthz must remain 200 during quarantine — "+
			"k8s liveness probes have no X-Agent-ID and a 403 here would crashloop the pod")
	assert.Contains(t, rec.Body.String(), `"status":"ok"`)
}

// TestQuarantineActiveBlocksAnonymousAPIv1 guards against over-correction:
// the H-03 fix must not regress H-01. Anonymous calls to `/api/v1/*` must
// still be denied while a quarantine is open.
func TestQuarantineActiveBlocksAnonymousAPIv1(t *testing.T) {
	cb := breakerWithThreshold(t)
	srv := testServerWithBreaker(t, cb)
	cb.RecordViolation(context.Background(), "agent-bad", security.ViolationCapability)
	require.True(t, cb.IsQuarantined("agent-bad"))

	// An anonymous call to a real API endpoint (DELETE on a nonexistent
	// agent — chosen because it does not require a request body and is
	// idempotent for test purposes) must still be denied.
	rec := doRequest(srv.Handler(), http.MethodDelete, "/api/v1/agents/some-agent", nil)
	assert.Equal(t, http.StatusForbidden, rec.Code,
		"H-01 protection must remain in place for /api/v1/* during quarantine")
	assert.Contains(t, rec.Body.String(), "QUARANTINE_ACTIVE")
}

// TestHealthzAllowedWithoutQuarantine sanity-checks the no-quarantine
// baseline so a regression in the route-mounting plumbing is unambiguously
// attributable to the fix (vs. a pre-existing /healthz break).
func TestHealthzAllowedWithoutQuarantine(t *testing.T) {
	cb := breakerWithThreshold(t)
	srv := testServerWithBreaker(t, cb)

	rec := doRequest(srv.Handler(), http.MethodGet, "/healthz", nil)
	assert.Equal(t, http.StatusOK, rec.Code)
}
