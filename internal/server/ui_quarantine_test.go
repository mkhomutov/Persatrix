package server

import (
	"context"
	"net/http"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/mkhomutov/persatrix/internal/security"
)

// RFC 0048 review finding (PR 2): the embedded web console is operator/tester
// traffic, not agent API traffic, so its surface must bypass the agent
// rate-limiter / circuit-breaker exactly like /healthz does (server.go Handler
// godoc, PR #244 H-03).
//
// Without the bypass, the console's boot endpoints (/api/v1/ui/config,
// /api/v1/ui/context) and static tree sit under /api/v1/ behind
// RESTRateLimitMiddleware. The browser calls them anonymously (no X-Agent-ID),
// so the PR #244 H-01 anonymous-deny — which fires whenever *any* agent is
// quarantined — would 403 them and make the console unbootable precisely when
// an operator most needs it to investigate or clear the quarantine.
//
// These tests pin the bypass: the console surface stays reachable during an
// active quarantine, while genuine /api/v1/* agent endpoints remain denied (the
// H-01 protection must not regress).

// uiTestServerWithBreaker builds a console-wired Server (WithUI) behind a
// circuit breaker, mirroring testServerWithBreaker's post-construction
// WithRateLimiter application.
func uiTestServerWithBreaker(t *testing.T, cb *security.CircuitBreaker, opts ...ServerOption) *Server {
	t.Helper()
	srv := uiTestServer(t, append([]ServerOption{WithUI(uiAssetFS())}, opts...)...)
	WithRateLimiter(nil, cb)(srv)
	return srv
}

// TestUIConfig_AllowedDuringQuarantine is the load-bearing assertion: an
// anonymous GET /api/v1/ui/config must still serve 200 while a quarantine is
// open, so the console can boot and an operator can act on the incident.
func TestUIConfig_AllowedDuringQuarantine(t *testing.T) {
	cb := breakerWithThreshold(t)
	srv := uiTestServerWithBreaker(t, cb)

	cb.RecordViolation(context.Background(), "agent-bad", security.ViolationCapability)
	require.True(t, cb.IsQuarantined("agent-bad"),
		"precondition: agent-bad must be quarantined to exercise the bypass")

	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/ui/config", nil)
	assert.Equal(t, http.StatusOK, rec.Code,
		"the console's config boot endpoint must bypass the agent breaker — "+
			"a quarantine must not make the operator console unbootable")
}

// TestUIContext_AllowedDuringQuarantine pins the same bypass for the identity
// boot endpoint.
func TestUIContext_AllowedDuringQuarantine(t *testing.T) {
	cb := breakerWithThreshold(t)
	srv := uiTestServerWithBreaker(t, cb)

	cb.RecordViolation(context.Background(), "agent-bad", security.ViolationCapability)
	require.True(t, cb.IsQuarantined("agent-bad"))

	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/ui/context", nil)
	assert.Equal(t, http.StatusOK, rec.Code,
		"the console's context boot endpoint must bypass the agent breaker")
}

// TestUIStatic_AllowedDuringQuarantine confirms the static console tree is
// reachable during a quarantine too — boot endpoints serving 200 is useless if
// the SPA shell itself 403s.
func TestUIStatic_AllowedDuringQuarantine(t *testing.T) {
	cb := breakerWithThreshold(t)
	srv := uiTestServerWithBreaker(t, cb)

	cb.RecordViolation(context.Background(), "agent-bad", security.ViolationCapability)
	require.True(t, cb.IsQuarantined("agent-bad"))

	rec := doRequest(srv.Handler(), http.MethodGet, "/ui/", nil)
	assert.Equal(t, http.StatusOK, rec.Code,
		"the static console shell must bypass the agent breaker")
}

// TestQuarantineStillBlocksAgentAPIWithConsole guards against over-correction:
// wiring the console must not regress the H-01 anonymous-deny for genuine
// /api/v1/* agent endpoints. With the same server and an active quarantine, an
// anonymous agent-API call must still be denied.
func TestQuarantineStillBlocksAgentAPIWithConsole(t *testing.T) {
	cb := breakerWithThreshold(t)
	srv := uiTestServerWithBreaker(t, cb)

	cb.RecordViolation(context.Background(), "agent-bad", security.ViolationCapability)
	require.True(t, cb.IsQuarantined("agent-bad"))

	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/agents", nil)
	assert.Equal(t, http.StatusForbidden, rec.Code,
		"the console bypass must be scoped to /ui — agent API endpoints keep the H-01 deny")
	assert.Contains(t, rec.Body.String(), "QUARANTINE_ACTIVE")
}
