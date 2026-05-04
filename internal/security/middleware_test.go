package security

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync"
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

// fakeServerTransportStream is the minimal [grpc.ServerTransportStream]
// needed for `grpc.SetHeader` to succeed inside a unit test. The real
// stream is owned by the gRPC runtime; tests construct one and attach it
// to the context via [grpc.NewContextWithServerTransportStream].
type fakeServerTransportStream struct {
	grpc.ServerTransportStream
	mu     sync.Mutex
	header metadata.MD
}

func (f *fakeServerTransportStream) Method() string { return "" }

func (f *fakeServerTransportStream) SetHeader(md metadata.MD) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.header == nil {
		f.header = metadata.MD{}
	}
	for k, v := range md {
		f.header[k] = append(f.header[k], v...)
	}
	return nil
}

func (f *fakeServerTransportStream) SendHeader(md metadata.MD) error { return f.SetHeader(md) }
func (f *fakeServerTransportStream) SetTrailer(md metadata.MD) error { return nil }

func (f *fakeServerTransportStream) get(key string) []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.header.Get(key)
}

// TestGRPCInterceptor_RetryAfterHeaderOnRateLimit guards PR #244 review
// M-04: gRPC `ResourceExhausted` responses must carry a
// `retry-after-seconds` header in parity with the REST `Retry-After`
// response header so clients can back off intelligently.
func TestGRPCInterceptor_RetryAfterHeaderOnRateLimit(t *testing.T) {
	clk := newFakeClock(time.Unix(0, 0))
	rl := newTestRateLimiter(t, clk, func(c *RateLimitConfig) {
		c.CallsPerWindow = 1
		c.WindowSeconds = 42 // distinctive value to assert against
	})
	cb, _ := newTestBreaker(t, clk)
	interceptor := GRPCRateLimitInterceptor(rl, cb)
	handler := func(ctx context.Context, req any) (any, error) { return "ok", nil }

	stream := &fakeServerTransportStream{}
	ctx := grpc.NewContextWithServerTransportStream(
		metadata.NewIncomingContext(context.Background(),
			metadata.Pairs("x-agent-id", "agent-a")),
		stream,
	)
	info := &grpc.UnaryServerInfo{FullMethod: "/svc/M"}

	// First call admitted.
	_, err := interceptor(ctx, nil, info, handler)
	require.NoError(t, err)
	// Second call denied — must set the retry-after-seconds header.
	_, err = interceptor(ctx, nil, info, handler)
	require.Error(t, err)
	st, ok := status.FromError(err)
	require.True(t, ok)
	require.Equal(t, codes.ResourceExhausted, st.Code())

	got := stream.get("retry-after-seconds")
	require.Len(t, got, 1, "retry-after-seconds header must be set on rate-limit deny")
	assert.Equal(t, "42", got[0])
}

// TestRESTMiddleware_AnonymousAllowedWhenNoQuarantine guards the
// baseline behaviour the H-01 fix must preserve: when no agent is
// quarantined, anonymous calls (empty X-Agent-ID) still flow through
// the limiter via the anonymous bucket. This protects infrastructure
// probes such as Kubernetes liveness checks against /healthz that do
// not (and should not) carry an agent identity.
//
// Pairs with TestRESTMiddleware_QuarantineActiveBlocksAnonymous below
// to lock in the precise trade-off: anonymous traffic is admitted in
// the steady state but cut off the moment the breaker opens, closing
// the header-omission bypass without breaking the no-quarantine case.
func TestRESTMiddleware_AnonymousAllowedWhenNoQuarantine(t *testing.T) {
	clk := newFakeClock(time.Unix(0, 0))
	rl := newTestRateLimiter(t, clk)
	cb, _ := newTestBreaker(t, clk)
	mw := RESTRateLimitMiddleware(rl, cb)
	handler := mw(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	req := httptest.NewRequest(http.MethodGet, "/healthz", nil)
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	assert.Equal(t, http.StatusOK, rec.Code,
		"anonymous calls must succeed when no agent is quarantined")
}

// TestRESTMiddleware_QuarantineActiveBlocksAnonymous guards PR #244
// review H-01: a quarantined agent must not be able to bypass the
// quarantine simply by omitting the X-Agent-ID header (which would
// otherwise re-bucket them as "anonymous" and skip IsQuarantined). The
// fix denies all empty-ID calls with 403 whenever ANY agent is
// currently quarantined.
//
// This is the minimum-impact mitigation: under the much more common
// "nothing quarantined" steady state (TestRESTMiddleware_AnonymousAllowedWhenNoQuarantine),
// anonymous traffic still passes. The mode flips only when an active
// quarantine makes the bypass exploitable — the operator releasing the
// quarantine restores anonymous access.
//
// Stronger fixes (auth, signed identity headers) belong in RFC 0009
// Phase 4; this is the v0.3 stop-gap.
func TestRESTMiddleware_QuarantineActiveBlocksAnonymous(t *testing.T) {
	clk := newFakeClock(time.Unix(0, 0))
	rl := newTestRateLimiter(t, clk)
	cb, _ := newTestBreaker(t, clk)
	for i := 0; i < 3; i++ {
		cb.RecordViolation("agent-bad", ViolationCapability)
	}
	require.True(t, cb.IsQuarantined("agent-bad"),
		"precondition: agent-bad must be quarantined")

	mw := RESTRateLimitMiddleware(rl, cb)
	handler := mw(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))

	// Caller omits X-Agent-ID entirely.
	req := httptest.NewRequest(http.MethodPost, "/api/v1/work", nil)
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	assert.Equal(t, http.StatusForbidden, rec.Code,
		"empty X-Agent-ID must be denied while a quarantine is active")
	assert.Contains(t, rec.Body.String(), "QUARANTINE_ACTIVE",
		"response body should carry the QUARANTINE_ACTIVE error code")

	// Releasing the quarantine restores anonymous access (no lingering
	// global-deny state).
	require.True(t, cb.Unquarantine("agent-bad", "operator-test"))
	rec = httptest.NewRecorder()
	handler.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/api/v1/work", nil))
	assert.Equal(t, http.StatusOK, rec.Code,
		"anonymous traffic must resume once quarantine is cleared")
}

// TestGRPCInterceptor_QuarantineActiveBlocksAnonymous mirrors
// TestRESTMiddleware_QuarantineActiveBlocksAnonymous on the gRPC side.
// Maps to canonical gRPC code `PermissionDenied` (parity with the
// existing quarantine path, which also returns PermissionDenied).
func TestGRPCInterceptor_QuarantineActiveBlocksAnonymous(t *testing.T) {
	clk := newFakeClock(time.Unix(0, 0))
	rl := newTestRateLimiter(t, clk)
	cb, _ := newTestBreaker(t, clk)
	for i := 0; i < 3; i++ {
		cb.RecordViolation("agent-bad", ViolationCapability)
	}
	require.True(t, cb.IsQuarantined("agent-bad"))

	interceptor := GRPCRateLimitInterceptor(rl, cb)
	handler := func(ctx context.Context, req any) (any, error) { return "ok", nil }

	// No metadata at all -> empty agent ID.
	_, err := interceptor(context.Background(), nil, &grpc.UnaryServerInfo{}, handler)
	require.Error(t, err)
	st, _ := status.FromError(err)
	assert.Equal(t, codes.PermissionDenied, st.Code(),
		"empty x-agent-id metadata must be denied while quarantine is active")
}

// PR #244 round-2 review M-07 — bound + validate X-Agent-ID at the
// middleware boundary.
//
// X-Agent-ID is self-reported and flows directly into:
//   (a) audit log (per-event fsync for security-class events),
//   (b) the rate-limiter ring map key (memory),
//   (c) the breaker `violations` map key (memory).
//
// Without an upper bound, a single attacker can:
//   - send a 16 KiB X-Agent-ID and amplify per-call disk writes,
//   - send junk that bypasses the agent-ID schema enforced everywhere
//     else (`^[a-z0-9][a-z0-9-]*[a-z0-9]$`) and create rate-limit
//     bookkeeping for IDs the rest of the system (registry, path
//     handlers) will reject.
//
// The fix rejects with 400 / `INVALID_AGENT_ID` (REST) and
// `InvalidArgument` (gRPC) before the value reaches any sink. Empty
// stays admitted as the "anonymous bucket" — that is the existing
// contract and is what /healthz, probes, and pre-Phase-4 callers rely
// on.

func TestRESTMiddleware_RejectsOverlongAgentID(t *testing.T) {
	clk := newFakeClock(time.Unix(0, 0))
	rl := newTestRateLimiter(t, clk)
	cb, _ := newTestBreaker(t, clk)
	mw := RESTRateLimitMiddleware(rl, cb)
	handler := mw(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))

	// 257 chars — one over the 256 cap.
	overlong := strings.Repeat("a", MaxAgentIDLen+1)
	req := httptest.NewRequest(http.MethodPost, "/x", nil)
	req.Header.Set("X-Agent-ID", overlong)
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	assert.Equal(t, http.StatusBadRequest, rec.Code,
		"M-07: overlong X-Agent-ID must be rejected before reaching audit/map sinks")
	assert.Contains(t, rec.Body.String(), "INVALID_AGENT_ID")
}

func TestRESTMiddleware_RejectsMalformedAgentID(t *testing.T) {
	clk := newFakeClock(time.Unix(0, 0))
	rl := newTestRateLimiter(t, clk)
	cb, _ := newTestBreaker(t, clk)
	mw := RESTRateLimitMiddleware(rl, cb)
	handler := mw(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))

	// IDs that pass the rest of the stack would never reject — these
	// must be 400'd at the boundary instead of silently creating
	// bookkeeping entries the registry / path handlers will discard.
	cases := []struct {
		name string
		id   string
	}{
		{"uppercase", "Code-Writer"},
		{"underscore", "code_writer"},
		{"trailing dash", "writer-"},
		{"leading dash", "-writer"},
		{"dots", "code.writer"},
		{"single char", "a"}, // regex requires \u22652 chars
		{"slash", "a/b"},
		{"newline", "a\nb"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			req := httptest.NewRequest(http.MethodPost, "/x", nil)
			req.Header.Set("X-Agent-ID", tc.id)
			rec := httptest.NewRecorder()
			handler.ServeHTTP(rec, req)
			assert.Equal(t, http.StatusBadRequest, rec.Code)
			assert.Contains(t, rec.Body.String(), "INVALID_AGENT_ID")
		})
	}
}

func TestRESTMiddleware_AcceptsEmptyAndValidAgentID(t *testing.T) {
	// The validation must NOT regress the two admitted shapes:
	//   - "" (anonymous bucket — see TestRESTMiddleware_AnonymousAllowedWhenNoQuarantine)
	//   - any value matching `^[a-z0-9][a-z0-9-]*[a-z0-9]$`
	clk := newFakeClock(time.Unix(0, 0))
	rl := newTestRateLimiter(t, clk)
	cb, _ := newTestBreaker(t, clk)
	mw := RESTRateLimitMiddleware(rl, cb)
	handler := mw(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	for _, id := range []string{"", "agent-a", "ab", "x9", "operator"} {
		req := httptest.NewRequest(http.MethodPost, "/x", nil)
		if id != "" {
			req.Header.Set("X-Agent-ID", id)
		}
		rec := httptest.NewRecorder()
		handler.ServeHTTP(rec, req)
		assert.Equal(t, http.StatusOK, rec.Code, "id=%q must be admitted", id)
	}
}

func TestGRPCInterceptor_RejectsOverlongAgentID(t *testing.T) {
	clk := newFakeClock(time.Unix(0, 0))
	rl := newTestRateLimiter(t, clk)
	cb, _ := newTestBreaker(t, clk)
	interceptor := GRPCRateLimitInterceptor(rl, cb)
	handler := func(ctx context.Context, req any) (any, error) { return "ok", nil }
	overlong := strings.Repeat("a", MaxAgentIDLen+1)
	ctx := metadata.NewIncomingContext(context.Background(),
		metadata.Pairs("x-agent-id", overlong))
	_, err := interceptor(ctx, nil, &grpc.UnaryServerInfo{}, handler)
	require.Error(t, err)
	st, _ := status.FromError(err)
	assert.Equal(t, codes.InvalidArgument, st.Code(),
		"M-07: gRPC parity \u2014 overlong x-agent-id must be InvalidArgument")
}

func TestGRPCInterceptor_RejectsMalformedAgentID(t *testing.T) {
	clk := newFakeClock(time.Unix(0, 0))
	rl := newTestRateLimiter(t, clk)
	cb, _ := newTestBreaker(t, clk)
	interceptor := GRPCRateLimitInterceptor(rl, cb)
	handler := func(ctx context.Context, req any) (any, error) { return "ok", nil }
	ctx := metadata.NewIncomingContext(context.Background(),
		metadata.Pairs("x-agent-id", "Code-Writer"))
	_, err := interceptor(ctx, nil, &grpc.UnaryServerInfo{}, handler)
	require.Error(t, err)
	st, _ := status.FromError(err)
	assert.Equal(t, codes.InvalidArgument, st.Code())
}
