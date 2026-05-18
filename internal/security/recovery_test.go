package security

import (
	"context"
	"errors"
	"net"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
	"go.uber.org/zap/zapcore"
	"go.uber.org/zap/zaptest/observer"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"
)

// okHandler is a trivial unary handler used to drive interceptor tests
// that do not themselves panic.
func okHandler(_ context.Context, _ any) (any, error) { return "ok", nil }

// TestGRPCRecoveryInterceptor_RecoversHandlerPanic pins the core
// contract from ISSUE-0059: a panicking unary handler must not escape
// the per-RPC goroutine (which would crash the orchestrator process).
// The interceptor converts the panic into a codes.Internal status
// error, mirroring the HTTP server's recoveryMiddleware.
func TestGRPCRecoveryInterceptor_RecoversHandlerPanic(t *testing.T) {
	interceptor := GRPCRecoveryInterceptor(zap.NewNop())
	handler := func(_ context.Context, _ any) (any, error) {
		panic("handler blew up")
	}
	info := &grpc.UnaryServerInfo{FullMethod: "/wallet.WalletService/AcquireLease"}

	resp, err := interceptor(context.Background(), nil, info, handler)

	require.Error(t, err, "panic must surface as an error, not propagate")
	assert.Nil(t, resp, "no response value on a recovered panic")
	st, ok := status.FromError(err)
	require.True(t, ok, "recovered panic must be a gRPC status error")
	assert.Equal(t, codes.Internal, st.Code())
}

// TestGRPCRecoveryInterceptor_PassesThroughSuccess confirms the
// interceptor is transparent on the happy path — a normal return is
// forwarded unchanged.
func TestGRPCRecoveryInterceptor_PassesThroughSuccess(t *testing.T) {
	interceptor := GRPCRecoveryInterceptor(zap.NewNop())
	resp, err := interceptor(context.Background(), nil, &grpc.UnaryServerInfo{}, okHandler)
	require.NoError(t, err)
	assert.Equal(t, "ok", resp)
}

// TestGRPCRecoveryInterceptor_PassesThroughHandlerError confirms a
// handler error is forwarded verbatim — the interceptor must not
// rewrite a deliberate handler error into codes.Internal. Both a plain
// error and a typed status error are checked.
func TestGRPCRecoveryInterceptor_PassesThroughHandlerError(t *testing.T) {
	interceptor := GRPCRecoveryInterceptor(zap.NewNop())

	sentinel := errors.New("deliberate handler error")
	_, err := interceptor(context.Background(), nil, &grpc.UnaryServerInfo{},
		func(_ context.Context, _ any) (any, error) { return nil, sentinel })
	require.ErrorIs(t, err, sentinel, "plain handler error must pass through unchanged")

	denied := status.Error(codes.PermissionDenied, "agent is quarantined")
	_, err = interceptor(context.Background(), nil, &grpc.UnaryServerInfo{},
		func(_ context.Context, _ any) (any, error) { return nil, denied })
	st, ok := status.FromError(err)
	require.True(t, ok)
	assert.Equal(t, codes.PermissionDenied, st.Code(),
		"a handler's own status code must not be rewritten to Internal")
}

// TestGRPCRecoveryInterceptor_LogsPanicWithStackAndMethod verifies the
// recovered panic is logged at Error level with the panic value, the
// RPC method, and a stack trace — the operator-visibility parity with
// recoveryMiddleware's panic log entry.
func TestGRPCRecoveryInterceptor_LogsPanicWithStackAndMethod(t *testing.T) {
	core, logs := observer.New(zapcore.DebugLevel)
	interceptor := GRPCRecoveryInterceptor(zap.New(core))
	handler := func(_ context.Context, _ any) (any, error) {
		panic("handler blew up")
	}
	info := &grpc.UnaryServerInfo{FullMethod: "/wallet.WalletService/AcquireLease"}

	_, err := interceptor(context.Background(), nil, info, handler)
	require.Error(t, err)

	panicLogs := logs.FilterMessage("gRPC handler panic").All()
	require.Len(t, panicLogs, 1, "exactly one panic log entry expected")
	entry := panicLogs[0]
	assert.Equal(t, zapcore.ErrorLevel, entry.Level, "panic must log at Error level")
	fields := entry.ContextMap()
	assert.Equal(t, "/wallet.WalletService/AcquireLease", fields["method"])
	assert.Contains(t, fields["panic"], "handler blew up")
	assert.NotEmpty(t, fields["stack"], "a stack trace must be captured")
}

// TestGRPCRecoveryInterceptor_NilLoggerDoesNotPanic guards the
// defensive path: a nil *zap.Logger must not turn the recovery handler
// itself into a second (fatal) panic. The whole point of the
// interceptor is that it never crashes the process.
func TestGRPCRecoveryInterceptor_NilLoggerDoesNotPanic(t *testing.T) {
	interceptor := GRPCRecoveryInterceptor(nil)
	handler := func(_ context.Context, _ any) (any, error) {
		panic("handler blew up")
	}
	resp, err := interceptor(context.Background(), nil, &grpc.UnaryServerInfo{}, handler)
	assert.Nil(t, resp)
	st, ok := status.FromError(err)
	require.True(t, ok)
	assert.Equal(t, codes.Internal, st.Code())
}

// TestGRPCRecoveryInterceptor_CatchesPanicFromDownstreamInterceptor
// pins the ISSUE-0059 requirement that the recovery interceptor is
// composed as the OUTERMOST link of grpc.ChainUnaryInterceptor: a panic
// raised inside a later interceptor (e.g. the rate-limiter) must also
// be caught. The chain semantics are reproduced here by invoking the
// downstream interceptor from inside the handler closure the recovery
// interceptor receives — exactly how grpc-go nests a chain.
func TestGRPCRecoveryInterceptor_CatchesPanicFromDownstreamInterceptor(t *testing.T) {
	recovery := GRPCRecoveryInterceptor(zap.NewNop())
	downstream := func(_ context.Context, _ any, _ *grpc.UnaryServerInfo, _ grpc.UnaryHandler) (any, error) {
		panic("downstream interceptor blew up")
	}
	info := &grpc.UnaryServerInfo{FullMethod: "/svc/M"}

	resp, err := recovery(context.Background(), nil, info,
		func(ctx context.Context, req any) (any, error) {
			return downstream(ctx, req, info, okHandler)
		})

	require.Error(t, err)
	assert.Nil(t, resp)
	st, ok := status.FromError(err)
	require.True(t, ok)
	assert.Equal(t, codes.Internal, st.Code(),
		"a panic in a downstream interceptor must be caught by the outermost recovery interceptor")
}

// panicHealthServer is a minimal healthpb.HealthServer whose Check
// always panics — it drives a handler panic through a real in-process
// gRPC server in the end-to-end test below.
type panicHealthServer struct {
	healthpb.UnimplementedHealthServer
}

func (panicHealthServer) Check(_ context.Context, _ *healthpb.HealthCheckRequest) (*healthpb.HealthCheckResponse, error) {
	panic("health handler blew up")
}

// TestGRPCRecoveryInterceptor_EndToEndRecoversHandlerPanic exercises the
// interceptor through a real bufconn gRPC server wired exactly as
// cmd/orchestrator/main.go wires it — via grpc.ChainUnaryInterceptor.
// A panicking handler must surface to the client as codes.Internal, and
// the server must stay up: a second call still gets a clean response,
// proving the panic did not crash the process.
func TestGRPCRecoveryInterceptor_EndToEndRecoversHandlerPanic(t *testing.T) {
	lis := bufconn.Listen(1 << 20)
	t.Cleanup(func() { _ = lis.Close() })

	srv := grpc.NewServer(grpc.ChainUnaryInterceptor(GRPCRecoveryInterceptor(zap.NewNop())))
	healthpb.RegisterHealthServer(srv, panicHealthServer{})
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
	for i := 0; i < 2; i++ {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		_, err := client.Check(ctx, &healthpb.HealthCheckRequest{})
		cancel()
		require.Error(t, err, "call %d: panicking handler must surface as an error", i)
		st, ok := status.FromError(err)
		require.True(t, ok, "call %d", i)
		assert.Equal(t, codes.Internal, st.Code(), "call %d", i)
	}
}
