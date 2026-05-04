package security

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strconv"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

func TestRESTMiddleware_429OnDeny(t *testing.T) {
	clk := newFakeClock(time.Unix(0, 0))
	rl := newTestRateLimiter(t, clk, func(c *RateLimitConfig) { c.CallsPerWindow = 2 })
	cb, _ := newTestBreaker(t, clk)
	mw := RESTRateLimitMiddleware(rl, cb)

	handler := mw(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))

	for i := 0; i < 2; i++ {
		req := httptest.NewRequest(http.MethodPost, "/x", nil)
		req.Header.Set("X-Agent-ID", "agent-a")
		rec := httptest.NewRecorder()
		handler.ServeHTTP(rec, req)
		require.Equal(t, http.StatusOK, rec.Code, "call %d", i)
	}
	req := httptest.NewRequest(http.MethodPost, "/x", nil)
	req.Header.Set("X-Agent-ID", "agent-a")
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	assert.Equal(t, http.StatusTooManyRequests, rec.Code)
	retry := rec.Header().Get("Retry-After")
	require.NotEmpty(t, retry, "Retry-After header required on 429")
	_, err := strconv.Atoi(retry)
	assert.NoError(t, err, "Retry-After should be an integer seconds value")
}

func TestRESTMiddleware_QuarantinedReturns403(t *testing.T) {
	clk := newFakeClock(time.Unix(0, 0))
	rl := newTestRateLimiter(t, clk)
	cb, _ := newTestBreaker(t, clk)
	for i := 0; i < 3; i++ {
		cb.RecordViolation("agent-a", ViolationCapability)
	}
	require.True(t, cb.IsQuarantined("agent-a"))
	mw := RESTRateLimitMiddleware(rl, cb)
	handler := mw(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	req := httptest.NewRequest(http.MethodPost, "/x", nil)
	req.Header.Set("X-Agent-ID", "agent-a")
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	assert.Equal(t, http.StatusForbidden, rec.Code)
}

func TestGRPCInterceptor_ResourceExhaustedOnDeny(t *testing.T) {
	clk := newFakeClock(time.Unix(0, 0))
	rl := newTestRateLimiter(t, clk, func(c *RateLimitConfig) { c.CallsPerWindow = 1 })
	cb, _ := newTestBreaker(t, clk)
	interceptor := GRPCRateLimitInterceptor(rl, cb)

	handler := func(ctx context.Context, req any) (any, error) { return "ok", nil }

	md := metadata.Pairs("x-agent-id", "agent-a")
	ctx := metadata.NewIncomingContext(context.Background(), md)
	info := &grpc.UnaryServerInfo{FullMethod: "/svc/M"}

	resp, err := interceptor(ctx, nil, info, handler)
	require.NoError(t, err)
	require.Equal(t, "ok", resp)

	_, err = interceptor(ctx, nil, info, handler)
	require.Error(t, err)
	st, ok := status.FromError(err)
	require.True(t, ok)
	assert.Equal(t, codes.ResourceExhausted, st.Code())
}

func TestGRPCInterceptor_QuarantinedReturnsPermissionDenied(t *testing.T) {
	clk := newFakeClock(time.Unix(0, 0))
	rl := newTestRateLimiter(t, clk)
	cb, _ := newTestBreaker(t, clk)
	for i := 0; i < 3; i++ {
		cb.RecordViolation("agent-a", ViolationCapability)
	}
	interceptor := GRPCRateLimitInterceptor(rl, cb)
	handler := func(ctx context.Context, req any) (any, error) { return "ok", nil }
	ctx := metadata.NewIncomingContext(context.Background(),
		metadata.Pairs("x-agent-id", "agent-a"))
	_, err := interceptor(ctx, nil, &grpc.UnaryServerInfo{}, handler)
	require.Error(t, err)
	st, _ := status.FromError(err)
	assert.Equal(t, codes.PermissionDenied, st.Code())
}

func TestMiddleware_DenyRecordsBreakerViolation(t *testing.T) {
	clk := newFakeClock(time.Unix(0, 0))
	rl := newTestRateLimiter(t, clk, func(c *RateLimitConfig) { c.CallsPerWindow = 1 })
	cb, _ := newTestBreaker(t, clk, func(c *CircuitBreakerConfig) {
		c.Thresholds = map[ViolationType]ThresholdRule{
			ViolationRateLimit: {Count: 2, Window: time.Hour},
		}
	})
	mw := RESTRateLimitMiddleware(rl, cb)
	handler := mw(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	for i := 0; i < 3; i++ {
		req := httptest.NewRequest(http.MethodPost, "/x", nil)
		req.Header.Set("X-Agent-ID", "agent-a")
		rec := httptest.NewRecorder()
		handler.ServeHTTP(rec, req)
	}
	assert.True(t, cb.IsQuarantined("agent-a"),
		"two rate-limit denials should open the breaker")
}
