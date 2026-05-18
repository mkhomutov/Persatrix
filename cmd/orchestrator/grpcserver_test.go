package main

import (
	"context"
	"net"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"

	"github.com/mkhomutov/persatrix/internal/observability/logbuffer"
)

// TestNewAgentGRPCServer confirms the extracted agent-facing gRPC server
// builder returns a usable server with both agent-facing services
// registered. The recovery + rate-limit interceptor behaviour itself is
// covered in internal/security; this test pins the wiring extracted from
// main() (ISSUE-0059 / ISSUE-0008).
func TestNewAgentGRPCServer(t *testing.T) {
	buf, err := logbuffer.New(logbuffer.Config{Dir: t.TempDir()}, zap.NewNop())
	require.NoError(t, err)
	t.Cleanup(func() { _ = buf.Close() })

	// nil rate limiter + breaker: GRPCRateLimitInterceptor is nil-safe,
	// so the interceptor chain still composes.
	srv := newAgentGRPCServer(buf, nil, nil, zap.NewNop())
	require.NotNil(t, srv)
	t.Cleanup(srv.Stop)

	// LogService + WalletService are both registered on the listener.
	assert.Len(t, srv.GetServiceInfo(), 2,
		"both LogService and WalletService must be registered on the agent-facing server")
}

// panicService is a healthpb.HealthServer whose unary Check and
// server-streaming Watch handlers both panic. It is registered on a
// server *built by newAgentGRPCServer* to prove the recovery
// interceptors are wired into the production builder — the interceptor
// unit tests in internal/security build their own servers, so a
// regression that dropped either chain from newAgentGRPCServer would
// slip past them.
type panicService struct {
	healthpb.UnimplementedHealthServer
}

func (panicService) Check(_ context.Context, _ *healthpb.HealthCheckRequest) (*healthpb.HealthCheckResponse, error) {
	panic("unary handler blew up")
}

func (panicService) Watch(_ *healthpb.HealthCheckRequest, _ healthpb.Health_WatchServer) error {
	panic("stream handler blew up")
}

// TestNewAgentGRPCServer_RecoversHandlerPanic confirms newAgentGRPCServer
// wires both recovery interceptors. Interceptors are server-wide, so a
// panicking service registered on the built server exercises them: a
// panic in any handler — unary (Check) or streaming (Watch) — must
// surface to the client as codes.Internal rather than crash the
// process. The streaming leg is the one ISSUE-0059's unary-only
// interceptor could not cover (LogService.StreamLogs is bidi-streaming).
func TestNewAgentGRPCServer_RecoversHandlerPanic(t *testing.T) {
	buf, err := logbuffer.New(logbuffer.Config{Dir: t.TempDir()}, zap.NewNop())
	require.NoError(t, err)
	t.Cleanup(func() { _ = buf.Close() })

	// nil rate limiter + breaker: GRPCRateLimitInterceptor is nil-safe.
	srv := newAgentGRPCServer(buf, nil, nil, zap.NewNop())
	healthpb.RegisterHealthServer(srv, panicService{})

	lis := bufconn.Listen(1 << 20)
	t.Cleanup(func() { _ = lis.Close() })
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

	// Unary path — GRPCRecoveryInterceptor in grpc.ChainUnaryInterceptor.
	_, err = client.Check(ctx, &healthpb.HealthCheckRequest{})
	require.Error(t, err, "unary handler panic must surface as an error")
	st, ok := status.FromError(err)
	require.True(t, ok)
	assert.Equal(t, codes.Internal, st.Code(),
		"newAgentGRPCServer must wire the unary recovery interceptor")

	// Streaming path — GRPCStreamRecoveryInterceptor in
	// grpc.ChainStreamInterceptor. The panic surfaces on the first Recv.
	stream, err := client.Watch(ctx, &healthpb.HealthCheckRequest{})
	require.NoError(t, err, "stream creation must succeed")
	_, err = stream.Recv()
	require.Error(t, err, "stream handler panic must surface as an error")
	st, ok = status.FromError(err)
	require.True(t, ok)
	assert.Equal(t, codes.Internal, st.Code(),
		"newAgentGRPCServer must wire the stream recovery interceptor")
}
