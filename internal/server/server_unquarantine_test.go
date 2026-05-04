package server

import (
	"net/http"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/security"
)

// Unit tests for handleUnquarantineAgent (PR #244 review NTH-02).
//
// The integration test in tests/integration/rate_limiter_integration_test.go
// covers the happy path (204 No Content + breaker state cleared). These unit
// tests exercise the negative paths that are currently uncovered:
//
//   (a) breaker not wired   → 503 Service Unavailable
//   (b) invalid agent ID    → 400 Bad Request
//   (c) agent not quarantined → 404 Not Found
//
// Keeping these at the unit layer (vs. integration) avoids paying the
// httptest.Server boot cost for what are pure handler-branch assertions.

// breakerWithThreshold builds a CircuitBreaker that opens after a single
// capability violation, used to seed quarantine state without relying on
// timing.
func breakerWithThreshold(t *testing.T) *security.CircuitBreaker {
	t.Helper()
	cb, err := security.NewCircuitBreaker(security.CircuitBreakerConfig{
		Thresholds: map[security.ViolationType]security.ThresholdRule{
			// Window: 0 is the documented "every previously recorded
			// violation is immediately expired" mode (see ThresholdRule
			// godoc); paired with Count: 1 the breaker opens on the
			// first call. Used here only to seed test state — see
			// circuitbreaker.go for production guidance.
			security.ViolationCapability: {Count: 1, Window: 0},
		},
		Logger: zap.NewNop(),
	})
	require.NoError(t, err)
	return cb
}

// testServerWithBreaker mirrors testServer but wires a CircuitBreaker
// (no rate limiter) so handler tests can exercise quarantine-related
// code paths without standing up the full middleware stack. Applying
// WithRateLimiter post-construction matches how the integration test
// composes options and keeps this file independent of the New(...)
// option signature.
func testServerWithBreaker(t *testing.T, cb *security.CircuitBreaker) *Server {
	t.Helper()
	srv, _ := testServer(t)
	WithRateLimiter(nil, cb)(srv)
	return srv
}

func TestHandleUnquarantineAgent_NoBreaker_503(t *testing.T) {
	srv, _ := testServer(t)
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/agents/some-agent/unquarantine", nil)
	assert.Equal(t, http.StatusServiceUnavailable, rec.Code)
	assert.Contains(t, rec.Body.String(), "circuit breaker not configured")
}

func TestHandleUnquarantineAgent_InvalidID_400(t *testing.T) {
	cb := breakerWithThreshold(t)
	srv := testServerWithBreaker(t, cb)
	tests := []struct {
		name string
		id   string
	}{
		{"uppercase", "Code-Writer"},
		{"underscore", "code_writer"},
		{"single char", "a"},
		{"starts with dash", "-writer"},
		{"ends with dash", "writer-"},
		{"with dots", "code.writer"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/agents/"+tc.id+"/unquarantine", nil)
			assert.Equal(t, http.StatusBadRequest, rec.Code)
			assert.Contains(t, rec.Body.String(), "invalid agent ID format")
		})
	}
}

func TestHandleUnquarantineAgent_NotQuarantined_404(t *testing.T) {
	cb := breakerWithThreshold(t)
	srv := testServerWithBreaker(t, cb)
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/agents/clean-agent/unquarantine", nil)
	assert.Equal(t, http.StatusNotFound, rec.Code)
	assert.Contains(t, rec.Body.String(), "agent is not quarantined")
}

// PR #244 review H-02: optional shared-secret stop-gap auth on the
// unquarantine endpoint. The endpoint undoes a security control and is
// otherwise unauthenticated until RFC 0009 Phase 4 lands. When the
// operator opts in by configuring a token via WithUnquarantineToken
// (sourced from SECURITY_UNQUARANTINE_TOKEN at startup), the handler
// requires a matching `Authorization: Bearer <token>` header. The
// comparison uses `crypto/subtle.ConstantTimeCompare` to avoid
// timing-side-channel leakage of the token byte-by-byte.
//
// The check is purely additive: when no token is configured the
// pre-PR behaviour is preserved (TestHandleUnquarantineAgent_NoBreaker_503
// and the integration-test happy path still pass without an auth
// header).
//
// All H-02 tests below also send `X-Agent-ID: operator` because the
// H-01 fix in the rate-limit middleware rejects anonymous calls while
// a quarantine is open (which these tests deliberately trigger). The
// operator identifying themselves is the intended pattern — the
// handler already records X-Agent-ID as the audit `actor` for forensic
// purposes.

const operatorAgentID = "operator"

func operatorHeaders(extra map[string]string) map[string]string {
	h := map[string]string{security.AgentIDHeader: operatorAgentID}
	for k, v := range extra {
		h[k] = v
	}
	return h
}

// quarantineAgentForTest seeds breaker state so the success branch can
// be exercised. Returns the agent ID used.
func quarantineAgentForTest(t *testing.T, cb *security.CircuitBreaker, id string) {
	t.Helper()
	cb.RecordViolation(id, security.ViolationCapability)
	require.True(t, cb.IsQuarantined(id), "precondition: %s must be quarantined", id)
}

func TestHandleUnquarantineAgent_TokenConfigured_MissingHeader_401(t *testing.T) {
	cb := breakerWithThreshold(t)
	srv, _ := testServer(t)
	WithRateLimiter(nil, cb)(srv)
	WithUnquarantineToken("s3cret")(srv)
	quarantineAgentForTest(t, cb, "agent-x")

	rec := doRequestWithHeaders(srv.Handler(), http.MethodPost,
		"/api/v1/agents/agent-x/unquarantine", nil, operatorHeaders(nil))
	assert.Equal(t, http.StatusUnauthorized, rec.Code)
	assert.Contains(t, rec.Body.String(), "UNAUTHORIZED")
	// Quarantine state must NOT be cleared on a failed auth.
	assert.True(t, cb.IsQuarantined("agent-x"),
		"failed auth must not release quarantine")
}

func TestHandleUnquarantineAgent_TokenConfigured_WrongToken_401(t *testing.T) {
	cb := breakerWithThreshold(t)
	srv, _ := testServer(t)
	WithRateLimiter(nil, cb)(srv)
	WithUnquarantineToken("s3cret")(srv)
	quarantineAgentForTest(t, cb, "agent-x")

	headers := operatorHeaders(map[string]string{"Authorization": "Bearer not-the-token"})
	rec := doRequestWithHeaders(srv.Handler(), http.MethodPost,
		"/api/v1/agents/agent-x/unquarantine", nil, headers)
	assert.Equal(t, http.StatusUnauthorized, rec.Code)
	assert.True(t, cb.IsQuarantined("agent-x"))
}

func TestHandleUnquarantineAgent_TokenConfigured_CorrectToken_204(t *testing.T) {
	cb := breakerWithThreshold(t)
	srv, _ := testServer(t)
	WithRateLimiter(nil, cb)(srv)
	WithUnquarantineToken("s3cret")(srv)
	quarantineAgentForTest(t, cb, "agent-x")

	headers := operatorHeaders(map[string]string{"Authorization": "Bearer s3cret"})
	rec := doRequestWithHeaders(srv.Handler(), http.MethodPost,
		"/api/v1/agents/agent-x/unquarantine", nil, headers)
	assert.Equal(t, http.StatusNoContent, rec.Code)
	assert.False(t, cb.IsQuarantined("agent-x"),
		"correct token must release the quarantine")
}

func TestHandleUnquarantineAgent_NoTokenConfigured_NoAuthRequired(t *testing.T) {
	// Baseline: with WithUnquarantineToken not applied, the endpoint
	// remains open (matches pre-PR-244 behaviour). This guards against
	// accidentally tightening behaviour when no token is configured.
	cb := breakerWithThreshold(t)
	srv := testServerWithBreaker(t, cb)
	quarantineAgentForTest(t, cb, "agent-x")
	rec := doRequestWithHeaders(srv.Handler(), http.MethodPost,
		"/api/v1/agents/agent-x/unquarantine", nil, operatorHeaders(nil))
	assert.Equal(t, http.StatusNoContent, rec.Code)
}
