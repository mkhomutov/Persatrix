package security

import (
	"net/http"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"google.golang.org/grpc/codes"
)

// ISSUE-0187 step 4 (RFC 0009 Phases 3–4 plan PR 8, design point G-7): an
// agent's REST calls take a budget of their own, apart from its wallet calls,
// and a REST call refused by that budget counts nothing toward a quarantine.
// Anyone who can post to a channel makes each member persona read it, so
// before this a burst of posts spent the persona's wallet budget and, past
// five refusals, quarantined it.

func TestRESTMiddleware_RefusalRecordsNoViolation(t *testing.T) {
	clk := newFakeClock(time.Unix(0, 0))
	rl := newTestRateLimiter(t, clk, func(c *RateLimitConfig) { c.CallsPerWindow = 1 })
	cb, _ := newTestBreaker(t, clk, func(c *CircuitBreakerConfig) {
		c.Thresholds = map[ViolationType]ThresholdRule{
			ViolationRateLimit: {Count: 2, Window: time.Hour},
		}
	})
	rest := RESTRateLimitMiddleware(rl, cb)(okREST)

	var refused int
	for i := 0; i < 10; i++ {
		if code, _ := callREST(rest, "agent-a"); code == http.StatusTooManyRequests {
			refused++
		}
	}
	require.Equal(t, 9, refused, "precondition: the budget refuses every call past the first")
	assert.Empty(t, cb.QuarantinedAgents(), "a REST call refused for the agent's budget is no violation")
}

func TestRateLimitHooks_RESTAndWalletBudgetsAreApart(t *testing.T) {
	clk := newFakeClock(time.Unix(0, 0))
	rl := newTestRateLimiter(t, clk, func(c *RateLimitConfig) { c.CallsPerWindow = 2 })
	cb, _ := newTestBreaker(t, clk)
	rest := RESTRateLimitMiddleware(rl, cb)(okREST)
	wallet := GRPCRateLimitInterceptor(rl, cb)

	for i := 0; i < 2; i++ {
		code, body := callREST(rest, "agent-a")
		require.Equal(t, http.StatusOK, code, body)
	}
	code, _ := callREST(rest, "agent-a")
	require.Equal(t, http.StatusTooManyRequests, code, "precondition: the REST budget is spent")

	assert.Equal(t, codes.OK, callGRPC(wallet, "agent-a"), "the wallet budget is untouched")
	assert.Equal(t, codes.OK, callGRPC(wallet, "agent-a"))
	assert.Equal(t, codes.ResourceExhausted, callGRPC(wallet, "agent-a"),
		"the wallet keeps a budget of the same size, its own")
}

func TestRESTMiddleware_WithAgentIDKeysOnTheVerifiedAgent(t *testing.T) {
	clk := newFakeClock(time.Unix(0, 0))
	rl := newTestRateLimiter(t, clk, func(c *RateLimitConfig) { c.CallsPerWindow = 1 })
	cb, _ := newTestBreaker(t, clk)
	// The server verifies a request that names agent-a; one that names
	// agent-b shows no proof of it.
	verified := func(r *http.Request) string {
		if r.Header.Get(AgentIDHeader) == "agent-a" {
			return "agent-a"
		}
		return ""
	}
	rest := RESTRateLimitMiddleware(rl, cb, WithAgentID(verified))(okREST)

	code, _ := callREST(rest, "agent-b")
	assert.Equal(t, http.StatusOK, code, "an unproven ID is counted in the shared anonymous budget")
	code, _ = callREST(rest, "agent-b")
	assert.Equal(t, http.StatusTooManyRequests, code, "which the first call spent")
	code, _ = callREST(rest, "")
	assert.Equal(t, http.StatusTooManyRequests, code, "the budget a caller with no ID uses")
	code, _ = callREST(rest, "agent-a")
	assert.Equal(t, http.StatusOK, code, "the verified agent's budget is its own")
}

func TestRESTMiddleware_WithAgentIDRefusesAQuarantinedVerifiedAgent(t *testing.T) {
	clk := newFakeClock(time.Unix(0, 0))
	rl := newTestRateLimiter(t, clk)
	cb := quarantinedBreaker(t, clk)
	// A token proves agent-bad, and the request sends no X-Agent-ID.
	rest := RESTRateLimitMiddleware(rl, cb,
		WithAgentID(func(*http.Request) string { return "agent-bad" }))(okREST)

	code, body := callREST(rest, "")
	assert.Equal(t, http.StatusForbidden, code)
	assert.Contains(t, body, `"QUARANTINED"`)
}

func TestRESTMiddleware_WithAgentIDStillRefusesAQuarantinedHeader(t *testing.T) {
	clk := newFakeClock(time.Unix(0, 0))
	rl := newTestRateLimiter(t, clk)
	cb := quarantinedBreaker(t, clk)
	// The request names agent-bad and proves nothing. Refusing it costs the
	// caller nothing it could not get back by dropping the header.
	rest := RESTRateLimitMiddleware(rl, cb,
		WithAgentID(func(*http.Request) string { return "" }))(okREST)

	code, body := callREST(rest, "agent-bad")
	assert.Equal(t, http.StatusForbidden, code)
	assert.Contains(t, body, `"QUARANTINED"`)
}
