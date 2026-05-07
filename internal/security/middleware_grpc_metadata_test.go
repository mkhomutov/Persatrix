package security

import (
	"context"
	"net"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"
)

// retryAfterHealthServer is the minimal [healthpb.HealthServer] needed
// to drive a unary RPC through the rate-limit interceptor end-to-end.
// Only Check is exercised; Watch and List come from the unimplemented
// embed.
type retryAfterHealthServer struct {
	healthpb.UnimplementedHealthServer
}

func (retryAfterHealthServer) Check(_ context.Context, _ *healthpb.HealthCheckRequest) (*healthpb.HealthCheckResponse, error) {
	return &healthpb.HealthCheckResponse{Status: healthpb.HealthCheckResponse_SERVING}, nil
}

// TestGRPCInterceptor_RetryAfterDeliveredAsHeaderNotTrailer pins the
// client-observable contract for the `retry-after-seconds` metadata
// emitted on `ResourceExhausted`. ISSUE-0002 captured a doc/code
// mismatch — the godoc said "trailer" while the implementation calls
// `grpc.SetHeader`. The pre-existing fakeServerTransportStream test
// asserted only that the server handler invoked SetHeader; it could
// not catch a future swap of SetHeader → SetTrailer.
//
// This test uses a real in-process gRPC server (bufconn) and inspects
// what the wire delivers via the standard `grpc.Header` / `grpc.Trailer`
// CallOptions. It locks in Option A from ISSUE-0002 (header parity
// with the REST `Retry-After` response header):
//
//   - the value MUST arrive in initial metadata (`grpc.Header(&md)`).
//   - the value MUST NOT arrive in trailing metadata (`grpc.Trailer(&md)`),
//     so client back-off code can rely on `Header()` alone.
func TestGRPCInterceptor_RetryAfterDeliveredAsHeaderNotTrailer(t *testing.T) {
	clk := newFakeClock(time.Unix(0, 0))
	rl := newTestRateLimiter(t, clk, func(c *RateLimitConfig) {
		c.CallsPerWindow = 1
		c.WindowSeconds = 42 // distinctive value to assert against
	})
	cb, _ := newTestBreaker(t, clk)

	lis := bufconn.Listen(1 << 20)
	t.Cleanup(func() { _ = lis.Close() })

	srv := grpc.NewServer(grpc.UnaryInterceptor(GRPCRateLimitInterceptor(rl, cb)))
	healthpb.RegisterHealthServer(srv, retryAfterHealthServer{})
	go func() { _ = srv.Serve(lis) }()
	t.Cleanup(srv.Stop)

	cc, err := grpc.NewClient(
		"passthrough://bufconn",
		grpc.WithContextDialer(func(_ context.Context, _ string) (net.Conn, error) {
			return lis.DialContext(context.Background())
		}),
		grpc.WithTransportCredentials(insecure.NewCredentials()),
	)
	require.NoError(t, err)
	t.Cleanup(func() { _ = cc.Close() })

	client := healthpb.NewHealthClient(cc)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	ctx = metadata.AppendToOutgoingContext(ctx, "x-agent-id", "agent-a")

	// First call admitted — drains the single-call window.
	_, err = client.Check(ctx, &healthpb.HealthCheckRequest{})
	require.NoError(t, err, "first call should be admitted")

	// Second call denied — capture both header and trailer metadata.
	var headerMD, trailerMD metadata.MD
	_, err = client.Check(ctx, &healthpb.HealthCheckRequest{},
		grpc.Header(&headerMD), grpc.Trailer(&trailerMD))
	require.Error(t, err, "second call should be rate-limited")
	st, ok := status.FromError(err)
	require.True(t, ok)
	require.Equal(t, codes.ResourceExhausted, st.Code())

	// Header MUST carry retry-after-seconds; this is the contract clients
	// reach for after seeing ResourceExhausted.
	gotHeader := headerMD.Get("retry-after-seconds")
	require.Len(t, gotHeader, 1,
		"retry-after-seconds must be delivered as initial metadata (grpc.Header), got header MD: %v",
		headerMD)
	assert.Equal(t, "42", gotHeader[0])

	// Trailer MUST NOT carry retry-after-seconds; if the implementation
	// ever flips to grpc.SetTrailer, this assertion catches it before
	// the godoc and existing fake-stream test agree on a wrong contract.
	gotTrailer := trailerMD.Get("retry-after-seconds")
	assert.Empty(t, gotTrailer,
		"retry-after-seconds must not be delivered as trailing metadata; trailer MD: %v",
		trailerMD)
}
