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
