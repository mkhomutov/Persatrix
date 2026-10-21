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
// so they would share the anonymous budget with every other caller that sends
// no ID, and a flood of such calls would 429 the console precisely when an
// operator most needs it. (Until ISSUE-0152's fix, a quarantine of any agent
// also refused every anonymous call outright.)
//
// These tests pin the bypass: with the anonymous budget spent and a
// quarantine open, the console surface stays reachable, while genuine
// /api/v1/* agent endpoints stay behind the limiter and breaker.

// uiTestServerWithBreaker builds a console-wired Server (WithUI) behind a
// circuit breaker and a limiter whose anonymous budget is already spent
// (spendAnonymousBudget), so a console route that went behind the limiter
// would get 429.
func uiTestServerWithBreaker(t *testing.T, cb *security.CircuitBreaker, opts ...ServerOption) *Server {
	t.Helper()
	srv := uiTestServer(t, append([]ServerOption{WithUI(uiAssetFS())}, opts...)...)
	spendAnonymousBudget(t, srv, cb)
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
// wiring the console must not take genuine /api/v1/* agent endpoints out from
// behind the breaker. With the same server and an active quarantine, the
// quarantined agent's own agent-API call must still be refused.
func TestQuarantineStillBlocksAgentAPIWithConsole(t *testing.T) {
	cb := breakerWithThreshold(t)
	srv := uiTestServerWithBreaker(t, cb)

	cb.RecordViolation(context.Background(), "agent-bad", security.ViolationCapability)
	require.True(t, cb.IsQuarantined("agent-bad"))

	rec := doRequestWithHeaders(srv.Handler(), http.MethodGet, "/api/v1/agents", nil,
		map[string]string{security.AgentIDHeader: "agent-bad"})
	assert.Equal(t, http.StatusForbidden, rec.Code,
		"the console bypass must be scoped to /ui — agent API endpoints keep the breaker")
	assert.Contains(t, rec.Body.String(), `"QUARANTINED"`)
}
