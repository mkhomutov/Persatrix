package main

import (
	"context"
	"errors"
	"net"
	"time"

	"go.opentelemetry.io/contrib/instrumentation/google.golang.org/grpc/otelgrpc"
	"go.uber.org/zap"
	"google.golang.org/grpc"
	"google.golang.org/grpc/keepalive"

	"github.com/mkhomutov/persatrix/internal/generated/logpb"
	"github.com/mkhomutov/persatrix/internal/generated/walletpb"
	"github.com/mkhomutov/persatrix/internal/observability/logbuffer"
	"github.com/mkhomutov/persatrix/internal/security"
	"github.com/mkhomutov/persatrix/internal/server"
	"github.com/mkhomutov/persatrix/internal/wallet"
)

// newAgentGRPCServer builds the agent-facing gRPC server — the listener
// that hosts LogService and, when the cost config loaded, the RFC 0023
// WalletService. Extracted from main() so the orchestrator entry point
// stays within the file-size budget (cf. ISSUE-0008); net.Listen stays in
// main(), and serveAgentGRPC below runs Serve and the stop.
//
// walletSvc is nil when the cost config failed to load — the wallet
// composes the budget enforcer, so without it budget enforcement is
// disabled and no WalletService is registered.
//
// PR #173 review Should-Fix #3 — the per-stream + per-server resource
// budget bounds a single misbehaving (or, until RFC 0009 auth lands,
// malicious) shipper:
//   - MaxRecvMsgSize: 8 MiB caps a single LogBatch on the wire
//     (BATCH_MAX=256 entries × ~few-KB each leaves generous headroom).
//   - MaxConcurrentStreams: 256 streams per HTTP/2 connection — well
//     above the realistic agent fleet, well below a DoS threshold.
//   - KeepaliveEnforcementPolicy: reject clients pinging more than once
//     per 30s without an outstanding stream (matches gRPC defaults,
//     made explicit so abuse is rejected rather than absorbed).
//
// ISSUE-0059 + RFC 0009 PR 2 — the unary interceptor chain is applied
// outer-to-inner:
//  1. GRPCRecoveryInterceptor (outermost) converts a handler panic into
//     codes.Internal instead of letting it escape the per-RPC goroutine
//     and crash the orchestrator process — parity with the HTTP
//     server's recoveryMiddleware. Placed first so it also catches a
//     panic raised inside the rate-limit interceptor.
//  2. GRPCRateLimitInterceptor applies the RFC 0009 per-agent rate
//     limit + circuit-breaker quarantine; maps deny outcomes to
//     ResourceExhausted / PermissionDenied; nil-safe when
//     SECURITY_RATE_LIMIT_ENABLED=false.
//
// ISSUE-0059 — the stream interceptor chain carries
// GRPCStreamRecoveryInterceptor. LogService.StreamLogs is bidi-streaming
// (and is LogService's only RPC), so the unary recovery interceptor
// cannot wrap it; without the stream variant a panic in StreamLogs would
// still escape the per-RPC goroutine and crash the orchestrator.
//
// TODO(rfc0009-phase4): add the rate-limiter's grpc.StreamInterceptor
// variant — GRPCRateLimitInterceptor is unary-only, so StreamLogs
// currently bypasses the per-agent limiter (PR #244 review NTH-01).
func newAgentGRPCServer(
	logBuf *logbuffer.Buffer,
	rateLimiter *security.RateLimiter,
	circuitBreaker *security.CircuitBreaker,
	walletSvc *wallet.WalletService,
	logger *zap.Logger,
) *grpc.Server {
	srv := grpc.NewServer(
		grpc.StatsHandler(otelgrpc.NewServerHandler()),
		grpc.MaxRecvMsgSize(8*1024*1024),
		grpc.MaxConcurrentStreams(256),
		grpc.KeepaliveEnforcementPolicy(keepalive.EnforcementPolicy{
			MinTime:             30 * time.Second,
			PermitWithoutStream: false,
		}),
		grpc.ChainUnaryInterceptor(
			security.GRPCRecoveryInterceptor(logger),
			security.GRPCRateLimitInterceptor(rateLimiter, circuitBreaker),
		),
		grpc.ChainStreamInterceptor(
			security.GRPCStreamRecoveryInterceptor(logger),
		),
	)
	logpb.RegisterLogServiceServer(srv, server.NewLogServiceServer(logBuf, logger))
	// RFC 0023 — the enforcing WalletService shares the agent-facing
	// listener with LogService. Registered only when the cost config
	// loaded; nil ⇒ budget enforcement is disabled and no wallet is served.
	if walletSvc != nil {
		walletpb.RegisterWalletServiceServer(srv, walletSvc)
	}
	return srv
}

// grpcStopGrace bounds the graceful part of the agent-facing server's stop
// (ISSUE-0176). A running agent's log shipper holds its LogService stream
// open for as long as the agent runs, so a graceful stop alone would wait
// for every agent to stop first. Past the grace the server stops hard,
// which ends the calls still open; the shippers reconnect. It stays under
// shutdownDrainTimeout, so the drain in main() still ends cleanly, and
// well under Docker's default 10 s stop timeout.
const grpcStopGrace = 5 * time.Second

// serveAgentGRPC serves srv on lis until ctx is cancelled, then stops it and
// returns: the gRPC counterpart of server.Start, and main() runs both the
// same way (ISSUE-0176). The stop is graceful first, so no new calls start
// and the ones in flight (a wallet lease call) finish, and hard once grace
// runs out. If Serve fails on its own, its error returns at once, with ctx
// still live.
func serveAgentGRPC(ctx context.Context, srv *grpc.Server, lis net.Listener, grace time.Duration, logger *zap.Logger) error {
	served := make(chan error, 1)
	go func() { served <- srv.Serve(lis) }()
	select {
	case err := <-served:
		return err
	case <-ctx.Done():
	}

	stopped := make(chan struct{})
	go func() {
		srv.GracefulStop()
		close(stopped)
	}()
	select {
	case <-stopped:
	case <-time.After(grace):
		logger.Warn("gRPC graceful stop timed out, stopping hard", zap.Duration("grace", grace))
		srv.Stop() // ends the calls still open, and with them the GracefulStop
	}
	// Serve returns nil once stopped, or ErrServerStopped when the stop
	// came before it started serving.
	if err := <-served; !errors.Is(err, grpc.ErrServerStopped) {
		return err
	}
	return nil
}
