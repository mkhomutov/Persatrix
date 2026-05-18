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
// NOTE: only unary RPCs are covered. A background goroutine (e.g. the
// RFC 0023 reaper) is never wrapped by a server interceptor and must
// carry its own defer/recover — see ISSUE-0059 piece (2). A future
// streaming RPC needs the stream-interceptor variant; cf. the
// TODO(rfc0009-phase4) on grpc.StreamInterceptor.
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
				// Named returns: overwrite whatever the panicking call
				// frame left behind with a clean Internal error.
				resp = nil
				err = status.Error(codes.Internal, "internal server error")
			}
		}()
		return handler(ctx, req)
	}
}
