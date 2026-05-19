package main

import (
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
// stays within the file-size budget (cf. ISSUE-0008); the listener
// lifecycle (net.Listen / Serve / GracefulStop) stays in main().
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
