package security

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

// ISSUE-0152 step 2: a quarantine refuses only the agent ID it names.
//
// The rate-limit hooks used to refuse every caller that sent no agent ID
// while any ID was quarantined (PR #244 review H-01), so that a quarantined
// agent could not drop its header and carry on. Agent IDs are self-reported,
// so one invented ID, tripped with about 65 quick requests, shut out the CLI
// and every REST call the agents make. A caller that drops its header now
// lands in the shared anonymous budget, which still caps it; a fresh
// invented ID gets around any rule keyed on IDs anyway, until RFC 0009
// Phase 4 makes IDs verifiable.

// quarantinedBreaker returns a test breaker with "agent-bad" quarantined.
func quarantinedBreaker(t *testing.T, clk *fakeClock) *CircuitBreaker {
	t.Helper()
	cb, _ := newTestBreaker(t, clk)
	for i := 0; i < 3; i++ {
		cb.RecordViolation(context.Background(), "agent-bad", ViolationCapability)
	}
	require.True(t, cb.IsQuarantined("agent-bad"), "precondition: agent-bad must be quarantined")
	return cb
}

// callREST sends one request through the REST hook and returns the status
// code and body.
func callREST(h http.Handler, agentID string) (int, string) {
	req := httptest.NewRequest(http.MethodPost, "/api/v1/channels/group:x/messages", nil)
	if agentID != "" {
		req.Header.Set(AgentIDHeader, agentID)
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec.Code, rec.Body.String()
}

// callGRPC sends one unary call through the gRPC hook and returns its code.
func callGRPC(interceptor grpc.UnaryServerInterceptor, agentID string) codes.Code {
	ctx := context.Background()
	if agentID != "" {
		ctx = metadata.NewIncomingContext(ctx, metadata.Pairs(AgentIDMetadataKey, agentID))
	}
	ok := func(context.Context, any) (any, error) { return "ok", nil }
	_, err := interceptor(ctx, nil, &grpc.UnaryServerInfo{FullMethod: "/svc/M"}, ok)
	return status.Code(err)
}

var okREST = http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
	w.WriteHeader(http.StatusOK)
})

func TestRateLimitHooks_QuarantineRefusesOnlyTheQuarantinedID(t *testing.T) {
	cases := []struct {
		name     string
		agentID  string
		wantREST int
		wantCode string // error code the REST body must carry; "" for none
		wantGRPC codes.Code
	}{
		{"a caller with no ID is admitted", "", http.StatusOK, "", codes.OK},
		{"another ID is admitted", "agent-good", http.StatusOK, "", codes.OK},
		{"the quarantined ID is refused", "agent-bad", http.StatusForbidden, `"QUARANTINED"`, codes.PermissionDenied},
	}
	clk := newFakeClock(time.Unix(0, 0))
	rl := newTestRateLimiter(t, clk)
	cb := quarantinedBreaker(t, clk)
	rest := RESTRateLimitMiddleware(rl, cb)(okREST)
	interceptor := GRPCRateLimitInterceptor(rl, cb)

	for _, tc := range cases {
		t.Run(tc.name+"/rest", func(t *testing.T) {
			code, body := callREST(rest, tc.agentID)
			assert.Equal(t, tc.wantREST, code, body)
			assert.NotContains(t, body, "QUARANTINE_ACTIVE",
				"no caller is refused because some other ID is quarantined")
			if tc.wantCode != "" {
				assert.Contains(t, body, tc.wantCode)
			}
		})
		t.Run(tc.name+"/grpc", func(t *testing.T) {
			assert.Equal(t, tc.wantGRPC, callGRPC(interceptor, tc.agentID))
		})
	}
}

// TestRateLimitHooks_NoIDKeepsItsSharedBudgetDuringQuarantine pins what
// still caps a caller that drops its header: the shared anonymous budget.
// Its refusals are rate-limit refusals, and they quarantine nothing, because
// the breaker counts violations only against a named ID.
func TestRateLimitHooks_NoIDKeepsItsSharedBudgetDuringQuarantine(t *testing.T) {
	const calls = 10 // twice the breaker's rate-limit threshold of 5
	transports := []struct {
		name string
		call func(rl *RateLimiter, cb *CircuitBreaker) (admitted, limited bool)
	}{
		{"rest", func(rl *RateLimiter, cb *CircuitBreaker) (bool, bool) {
			code, _ := callREST(RESTRateLimitMiddleware(rl, cb)(okREST), "")
			return code == http.StatusOK, code == http.StatusTooManyRequests
		}},
		{"grpc", func(rl *RateLimiter, cb *CircuitBreaker) (bool, bool) {
			code := callGRPC(GRPCRateLimitInterceptor(rl, cb), "")
			return code == codes.OK, code == codes.ResourceExhausted
		}},
	}
	for _, tr := range transports {
		t.Run(tr.name, func(t *testing.T) {
			clk := newFakeClock(time.Unix(0, 0))
			rl := newTestRateLimiter(t, clk, func(c *RateLimitConfig) { c.CallsPerWindow = 2 })
			cb := quarantinedBreaker(t, clk)

			var admitted, limited int
			for i := 0; i < calls; i++ {
				a, l := tr.call(rl, cb)
				if a {
					admitted++
				}
				if l {
					limited++
				}
			}
			assert.Equal(t, 2, admitted, "the shared anonymous budget admits its cap")
			assert.Equal(t, calls-2, limited, "every call over the cap is rate-limited, not refused for the quarantine")
			assert.ElementsMatch(t, []string{"agent-bad"}, cb.QuarantinedAgents(),
				"refusals of callers with no ID quarantine nothing")
		})
	}
}
