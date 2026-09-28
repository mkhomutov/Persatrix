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
	"google.golang.org/grpc/health"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"

	"github.com/mkhomutov/persatrix/internal/cost"
	"github.com/mkhomutov/persatrix/internal/generated/logpb"
	"github.com/mkhomutov/persatrix/internal/observability/logbuffer"
	"github.com/mkhomutov/persatrix/internal/wallet"
)

// testWalletService builds a minimal enforcing WalletService for the
// agent-facing-server wiring tests. The cost surface is exercised in
// internal/cost and internal/wallet; here it only needs to be a non-nil
// registrable servicer.
func testWalletService(t *testing.T) *wallet.WalletService {
	t.Helper()
	costCfg := &cost.CostConfig{
		Pricing: map[string]cost.ModelPricing{},
		Budgets: cost.BudgetThresholds{
			Global:      cost.GlobalBudget{MaxDailyUSD: 100},
			PerWorkflow: cost.PerWorkflowBudget{DefaultMaxUSD: 10},
			PerAgent:    cost.PerAgentBudget{DefaultMaxUSD: 5},
		},
	}
	counter := cost.NewTokenCounter(costCfg, zap.NewNop())
	enforcer := cost.NewBudgetEnforcer(counter, costCfg, zap.NewNop())
	return wallet.NewWalletService(counter, enforcer, wallet.DefaultConfig(), zap.NewNop())
}

// TestNewAgentGRPCServer confirms the extracted agent-facing gRPC server
// builder registers the right services: LogService always, and the
// RFC 0023 WalletService only when a wallet was built (i.e. the cost config
// loaded). The recovery + rate-limit interceptor behaviour itself is
// covered in internal/security; this test pins the wiring extracted from
// main() (ISSUE-0059 / ISSUE-0008).
func TestNewAgentGRPCServer(t *testing.T) {
	buf, err := logbuffer.New(logbuffer.Config{Dir: t.TempDir()}, zap.NewNop())
	require.NoError(t, err)
	t.Cleanup(func() { _ = buf.Close() })

	tests := []struct {
		name       string
		walletSvc  *wallet.WalletService
		wantSvcLen int
	}{
		{"with wallet", testWalletService(t), 2},
		{"without wallet (cost config absent)", nil, 1},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			// nil rate limiter + breaker: GRPCRateLimitInterceptor is
			// nil-safe, so the interceptor chain still composes.
			srv := newAgentGRPCServer(buf, nil, nil, tc.walletSvc, zap.NewNop())
			require.NotNil(t, srv)
			t.Cleanup(srv.Stop)
			assert.Len(t, srv.GetServiceInfo(), tc.wantSvcLen,
				"LogService is always registered; WalletService only with a wallet")
		})
	}
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
	// nil wallet (testAgentGRPCServer): the interceptors are server-wide, so
	// the panicService registered below exercises them regardless of it.
	srv := testAgentGRPCServer(t)
	healthpb.RegisterHealthServer(srv, panicService{})
	cc, _, _ := startAgentGRPC(t, srv, grpcStopGrace)
	client := healthpb.NewHealthClient(cc)

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	// Unary path — GRPCRecoveryInterceptor in grpc.ChainUnaryInterceptor.
	_, err := client.Check(ctx, &healthpb.HealthCheckRequest{})
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

// testAgentGRPCServer builds the agent-facing server with no wallet, no rate
// limiter and no breaker (all nil-safe), over a throwaway log buffer.
func testAgentGRPCServer(t *testing.T) *grpc.Server {
	t.Helper()
	buf, err := logbuffer.New(logbuffer.Config{Dir: t.TempDir()}, zap.NewNop())
	require.NoError(t, err)
	t.Cleanup(func() { _ = buf.Close() })
	srv := newAgentGRPCServer(buf, nil, nil, nil, zap.NewNop())
	t.Cleanup(srv.Stop) // frees a serveAgentGRPC that never stopped on its own
	return srv
}

// startAgentGRPC runs serveAgentGRPC on srv over bufconn, as runAgentGRPC
// runs it for main(). It returns a client connection, the cancel that begins
// the stop (runAgentGRPC's stop calls it), and the channel serveAgentGRPC's
// result arrives on.
func startAgentGRPC(t *testing.T, srv *grpc.Server, grace time.Duration) (*grpc.ClientConn, context.CancelFunc, <-chan error) {
	t.Helper()
	lis := bufconn.Listen(1 << 20)
	t.Cleanup(func() { _ = lis.Close() })
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	served := make(chan error, 1)
	go func() { served <- serveAgentGRPC(ctx, srv, lis, grace, zap.NewNop()) }()
	return dialBufconn(t, lis), cancel, served
}

// dialBufconn connects a client to lis, as an agent connects to the gRPC port.
func dialBufconn(t *testing.T, lis *bufconn.Listener) *grpc.ClientConn {
	t.Helper()
	cc, err := grpc.NewClient(
		"passthrough://bufconn",
		grpc.WithContextDialer(func(_ context.Context, _ string) (net.Conn, error) {
			return lis.DialContext(context.Background())
		}),
		grpc.WithTransportCredentials(insecure.NewCredentials()),
	)
	require.NoError(t, err)
	t.Cleanup(func() { _ = cc.Close() })
	return cc
}

// awaitServed returns serveAgentGRPC's result, failing the test if it has
// not returned within limit. runAgentGRPC's stop waits on exactly this
// return.
func awaitServed(t *testing.T, served <-chan error, limit time.Duration) error {
	t.Helper()
	select {
	case err := <-served:
		return err
	case <-time.After(limit):
		t.Fatalf("serveAgentGRPC had not returned %v later", limit)
		return nil
	}
}

// TestServeAgentGRPC_StopsOnCancel: once ctx is cancelled, serveAgentGRPC
// stops the server and returns. An idle agent connection does not hold it up.
func TestServeAgentGRPC_StopsOnCancel(t *testing.T) {
	srv := testAgentGRPCServer(t)
	healthpb.RegisterHealthServer(srv, health.NewServer())
	cc, cancel, served := startAgentGRPC(t, srv, grpcStopGrace)
	_, err := healthpb.NewHealthClient(cc).Check(context.Background(), &healthpb.HealthCheckRequest{})
	require.NoError(t, err, "one call, so an agent connection is open and idle")

	cancel()
	// Well inside the grace: with nothing in flight, the graceful stop
	// completes on its own.
	require.NoError(t, awaitServed(t, served, 2*time.Second))
}

// TestServeAgentGRPC_BoundsTheGracefulStop: a running agent's log shipper
// holds its StreamLogs stream open for as long as the agent runs, so a
// graceful stop alone waits for the agent to stop. Past the grace the server
// stops hard; the stream ends Unavailable, and the shipper reconnects.
func TestServeAgentGRPC_BoundsTheGracefulStop(t *testing.T) {
	cc, cancel, served := startAgentGRPC(t, testAgentGRPCServer(t), 100*time.Millisecond)
	stream, err := logpb.NewLogServiceClient(cc).StreamLogs(context.Background())
	require.NoError(t, err)
	entries := make([]*logpb.LogEntry, 32) // LogService acks every 32 entries
	for i := range entries {
		entries[i] = &logpb.LogEntry{Message: "still running"}
	}
	require.NoError(t, stream.Send(&logpb.LogBatch{AgentId: "shipper", Entries: entries}))
	_, err = stream.Recv()
	require.NoError(t, err, "the ack: the server is holding the stream")

	cancel()
	require.NoError(t, awaitServed(t, served, 3*time.Second))
	_, err = stream.Recv()
	assert.Equal(t, codes.Unavailable, status.Code(err), "the hard stop ends the held stream")
}

// slowCheck answers Check after pause, which it does not cut short when the
// call is cancelled: a wallet lease call still running when the stop begins.
// It closes returned as the handler returns. The test's end releases it.
type slowCheck struct {
	healthpb.UnimplementedHealthServer
	pause                      time.Duration
	started, release, returned chan struct{}
}

// newSlowCheck registers the release after srv's cleanup, so it runs first:
// srv.Stop can wait on a handler still running.
func newSlowCheck(t *testing.T, pause time.Duration) slowCheck {
	s := slowCheck{pause: pause, started: make(chan struct{}), release: make(chan struct{}), returned: make(chan struct{})}
	t.Cleanup(func() { close(s.release) })
	return s
}

func (s slowCheck) Check(context.Context, *healthpb.HealthCheckRequest) (*healthpb.HealthCheckResponse, error) {
	defer close(s.returned)
	close(s.started)
	select {
	case <-time.After(s.pause):
	case <-s.release:
	}
	return &healthpb.HealthCheckResponse{Status: healthpb.HealthCheckResponse_SERVING}, nil
}

// callSlowCheck starts a Check on cc and returns once check's handler is
// running, with the channel the call's result arrives on.
func callSlowCheck(t *testing.T, cc *grpc.ClientConn, check slowCheck) <-chan error {
	t.Helper()
	answered := make(chan error, 1)
	go func() {
		_, err := healthpb.NewHealthClient(cc).Check(context.Background(), &healthpb.HealthCheckRequest{})
		answered <- err
	}()
	select {
	case <-check.started:
	case <-time.After(5 * time.Second):
		t.Fatal("the call never reached the server")
	}
	return answered
}

// TestServeAgentGRPC_LetsCallsInFlightFinish: the stop is graceful first, so
// a call already running when it begins still finishes and answers.
func TestServeAgentGRPC_LetsCallsInFlightFinish(t *testing.T) {
	srv := testAgentGRPCServer(t)
	check := newSlowCheck(t, 200*time.Millisecond)
	healthpb.RegisterHealthServer(srv, check)
	cc, cancel, served := startAgentGRPC(t, srv, grpcStopGrace)
	answered := callSlowCheck(t, cc, check)

	cancel()
	require.NoError(t, awaitServed(t, served, 2*time.Second))
	require.NoError(t, <-answered, "the call in flight when the stop began answered")
}

// TestServeAgentGRPC_WaitsForHandlersAfterTheHardStop: a handler still
// running when the grace runs out has returned by the time serveAgentGRPC
// does, so what main() closes after the stop (the channel store, the audit
// log) is never closed under it.
func TestServeAgentGRPC_WaitsForHandlersAfterTheHardStop(t *testing.T) {
	srv := testAgentGRPCServer(t)
	check := newSlowCheck(t, 200*time.Millisecond)
	healthpb.RegisterHealthServer(srv, check)
	cc, cancel, served := startAgentGRPC(t, srv, 150*time.Millisecond) // under the pause, over half of it
	callSlowCheck(t, cc, check)

	cancel()
	require.NoError(t, awaitServed(t, served, 2*time.Second))
	select {
	case <-check.returned:
	default:
		t.Fatal("serveAgentGRPC returned while a handler the hard stop cancelled was still running")
	}
}

// TestServeAgentGRPC_AStuckHandlerDoesNotHoldTheStop: the stop stays bounded
// even by a handler that never returns. main() defers it with no timeout of
// its own, so an unbounded stop would keep the orchestrator from exiting.
func TestServeAgentGRPC_AStuckHandlerDoesNotHoldTheStop(t *testing.T) {
	srv := testAgentGRPCServer(t)
	check := newSlowCheck(t, time.Hour)
	healthpb.RegisterHealthServer(srv, check)
	cc, cancel, served := startAgentGRPC(t, srv, 50*time.Millisecond)
	callSlowCheck(t, cc, check)

	cancel()
	require.NoError(t, awaitServed(t, served, 2*time.Second))
}

// TestServeAgentGRPC_ReturnsServeFailure: when Serve fails on its own,
// serveAgentGRPC returns the error at once, with ctx still live, so
// runAgentGRPC hands it to main(), which logs it and cancels the rest of the
// orchestrator.
func TestServeAgentGRPC_ReturnsServeFailure(t *testing.T) {
	srv := testAgentGRPCServer(t)
	lis := bufconn.Listen(1 << 20)
	require.NoError(t, lis.Close()) // Accept fails at once
	served := make(chan error, 1)
	go func() { served <- serveAgentGRPC(context.Background(), srv, lis, grpcStopGrace, zap.NewNop()) }()
	assert.Error(t, awaitServed(t, served, 2*time.Second))
}

// TestServeAgentGRPC_AStopBeforeServeIsNoFailure: Serve on a server already
// stopped returns grpc.ErrServerStopped. That is a normal stop, not a failure
// for main() to log and answer by cancelling the orchestrator.
func TestServeAgentGRPC_AStopBeforeServeIsNoFailure(t *testing.T) {
	srv := testAgentGRPCServer(t)
	srv.Stop()
	served := make(chan error, 1)
	go func() {
		served <- serveAgentGRPC(context.Background(), srv, bufconn.Listen(1<<20), grpcStopGrace, zap.NewNop())
	}()
	assert.NoError(t, awaitServed(t, served, 2*time.Second))
}

// TestRunAgentGRPC_ServesUntilStopped: main() defers the stop runAgentGRPC
// returns so that it runs after the HTTP and channel-fanout drains, whose
// persona turns still lease from the wallet (ISSUE-0176). Until then the
// server takes new calls; the stop returns once the server has stopped.
func TestRunAgentGRPC_ServesUntilStopped(t *testing.T) {
	srv := testAgentGRPCServer(t)
	healthpb.RegisterHealthServer(srv, health.NewServer())
	lis := bufconn.Listen(1 << 20)
	stop := runAgentGRPC(srv, lis, grpcStopGrace, zap.NewNop(), func(err error) { t.Errorf("Serve failed: %v", err) })
	client := healthpb.NewHealthClient(dialBufconn(t, lis))
	_, err := client.Check(context.Background(), &healthpb.HealthCheckRequest{})
	require.NoError(t, err, "a new call before the stop is served")

	stopped := make(chan error, 1)
	go func() { stop(); stopped <- nil }()
	require.NoError(t, awaitServed(t, stopped, 2*time.Second))
	_, err = client.Check(context.Background(), &healthpb.HealthCheckRequest{})
	assert.Equal(t, codes.Unavailable, status.Code(err), "no call is served after the stop")
}

// TestRunAgentGRPC_ReportsAServeFailure: a Serve that fails on its own goes
// to onFail at once (main() logs it and cancels the orchestrator), and the
// stop still returns.
func TestRunAgentGRPC_ReportsAServeFailure(t *testing.T) {
	lis := bufconn.Listen(1 << 20)
	require.NoError(t, lis.Close()) // Accept fails at once
	failed := make(chan error, 1)
	stop := runAgentGRPC(testAgentGRPCServer(t), lis, grpcStopGrace, zap.NewNop(), func(err error) { failed <- err })
	assert.Error(t, awaitServed(t, failed, 2*time.Second))

	stopped := make(chan error, 1)
	go func() { stop(); stopped <- nil }()
	require.NoError(t, awaitServed(t, stopped, 2*time.Second))
}
