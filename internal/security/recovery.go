package security

import (
	"context"
	"runtime/debug"

	"go.uber.org/zap"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// GRPCRecoveryInterceptor returns a unary server interceptor that
// recovers from a panic in any downstream handler (or downstream
// interceptor), logs the panic value and a stack trace, and converts it
// into a codes.Internal status error.
//
// gRPC-go does not recover handler panics by default — an unrecovered
// panic escapes the per-RPC goroutine and crashes the whole process.
// This interceptor is the gRPC-side counterpart of the HTTP server's
// recoveryMiddleware (internal/server/middleware.go), giving the
// agent-facing gRPC listener (LogService + WalletService) the same
// crash-isolation guarantee. See ISSUE-0059.
//
// Compose it as the OUTERMOST link of grpc.ChainUnaryInterceptor so it
// also catches a panic raised inside a later interceptor (e.g. the
// rate-limiter):
//
//	grpc.ChainUnaryInterceptor(
//	    security.GRPCRecoveryInterceptor(logger),
//	    security.GRPCRateLimitInterceptor(limiter, breaker),
//	)
//
// NOTE: this covers unary RPCs only. Streaming RPCs — LogService's
// StreamLogs, the agent-facing listener's one streaming surface — need
// the GRPCStreamRecoveryInterceptor companion below; a unary interceptor
// cannot wrap a streaming handler. A background goroutine (e.g. the
// RFC 0023 reaper) is never wrapped by a server interceptor at all and
// must carry its own defer/recover — see ISSUE-0059 piece (2).
func GRPCRecoveryInterceptor(logger *zap.Logger) grpc.UnaryServerInterceptor {
	return func(ctx context.Context, req any, info *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (resp any, err error) {
		defer func() {
			if rec := recover(); rec != nil {
				method := "unknown"
				if info != nil {
					method = info.FullMethod
				}
				// nil-safe: the interceptor's purpose is to never crash
				// the process — a missing logger must not turn the
				// recovery path into a second, fatal panic.
				if logger != nil {
					logger.Error("gRPC handler panic",
						zap.Any("panic", rec),
						zap.String("method", method),
						zap.String("stack", string(debug.Stack())),
					)
				}
				// Named returns: a panic aborts `return handler(...)`
				// before its assignment runs, so resp/err are still
				// zero — set them to a clean Internal error.
				resp = nil
				err = status.Error(codes.Internal, "internal server error")
			}
		}()
		return handler(ctx, req)
	}
}

// GRPCStreamRecoveryInterceptor is the streaming-RPC counterpart of
// GRPCRecoveryInterceptor: it recovers from a panic in any downstream
// stream handler (or downstream stream interceptor), logs the panic
// value and a stack trace, and converts it into a codes.Internal status
// error.
//
// A unary interceptor cannot wrap a streaming handler — and the
// agent-facing gRPC listener's LogService exposes exactly one RPC,
// StreamLogs, which is bidi-streaming. Without this guard a panic inside
// StreamLogs (e.g. while decoding an untrusted LogBatch) would escape
// the per-RPC goroutine and crash the orchestrator process, leaving the
// unary GRPCRecoveryInterceptor covering only WalletService. See
// ISSUE-0059.
//
// Compose it as the OUTERMOST link of grpc.ChainStreamInterceptor, for
// the same reason as the unary variant — so it also catches a panic
// raised inside a later stream interceptor:
//
//	grpc.ChainStreamInterceptor(
//	    security.GRPCStreamRecoveryInterceptor(logger),
//	)
func GRPCStreamRecoveryInterceptor(logger *zap.Logger) grpc.StreamServerInterceptor {
	return func(srv any, ss grpc.ServerStream, info *grpc.StreamServerInfo, handler grpc.StreamHandler) (err error) {
		defer func() {
			if rec := recover(); rec != nil {
				method := "unknown"
				if info != nil {
					method = info.FullMethod
				}
				// nil-safe for the same reason as the unary variant:
				// the recovery path must never panic a second time.
				if logger != nil {
					logger.Error("gRPC stream handler panic",
						zap.Any("panic", rec),
						zap.String("method", method),
						zap.String("stack", string(debug.Stack())),
					)
				}
				// Named return: a panic aborts `return handler(...)`
				// before its assignment runs, so err is still nil —
				// set it to a clean Internal error.
				err = status.Error(codes.Internal, "internal server error")
			}
		}()
		return handler(srv, ss)
	}
}
