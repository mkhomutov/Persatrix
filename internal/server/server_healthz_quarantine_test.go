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
// The PR #244 H-01 fix denied anonymous (empty `X-Agent-ID`) requests
// while any agent was quarantined. Because the rate-limit middleware was
// originally mounted on the root handler, that deny also caught
// `/healthz` — Kubernetes liveness probes do not (and should not)
// carry `X-Agent-ID`, so a single quarantined agent would crashloop the
// orchestrator (probe → 403 → pod restart → in-memory quarantine state
// lost → traffic resumes → re-quarantine → loop). ISSUE-0152 removed
// that deny; `/healthz` stays off the limiter anyway, because a probe
// sharing the anonymous budget could still be 429'd by a flood.
//
// These tests pin the bypass: `/healthz` must always return 200, even
// with the anonymous budget spent and a quarantine open, while `/api/v1/*`
// stays behind the limiter and breaker.

// TestHealthzAllowedDuringQuarantine asserts the H-03 fix: probes pass
// through untouched even when an agent is quarantined.
func TestHealthzAllowedDuringQuarantine(t *testing.T) {
	cb := breakerWithThreshold(t)
	srv := testServerWithBreaker(t, cb)
	spendAnonymousBudget(t, srv, cb)

	// Seed quarantine state, which once made the middleware deny
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

// TestQuarantinedAgentStillBlockedOnAPIv1 guards against over-correction:
// the H-03 bypass must not take `/api/v1/*` out from behind the breaker.
// The quarantined agent's own calls must still be refused, while an
// anonymous call is answered by the handler (ISSUE-0152).
func TestQuarantinedAgentStillBlockedOnAPIv1(t *testing.T) {
	cb := breakerWithThreshold(t)
	srv := testServerWithBreaker(t, cb)
	cb.RecordViolation(context.Background(), "agent-bad", security.ViolationCapability)
	require.True(t, cb.IsQuarantined("agent-bad"))

	// A real API endpoint (DELETE on a nonexistent agent — chosen
	// because it does not require a request body and is idempotent for
	// test purposes).
	rec := doRequestWithHeaders(srv.Handler(), http.MethodDelete, "/api/v1/agents/some-agent", nil,
		map[string]string{security.AgentIDHeader: "agent-bad"})
	assert.Equal(t, http.StatusForbidden, rec.Code,
		"the breaker must remain in place for /api/v1/* during quarantine")
	assert.Contains(t, rec.Body.String(), `"QUARANTINED"`)

	rec = doRequest(srv.Handler(), http.MethodDelete, "/api/v1/agents/some-agent", nil)
	assert.Equal(t, http.StatusNotFound, rec.Code,
		"an anonymous call reaches the handler while another ID is quarantined")
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
