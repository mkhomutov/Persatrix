// Package security: REST + gRPC rate-limit middleware (RFC 0009 PR 2).
package security

import (
	"context"
	"encoding/json"
	"net/http"
	"strconv"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

const (
	// AgentIDHeader is the canonical REST header carrying the caller's
	// self-reported agent ID (RFC 0009 §B). Self-reported until token
	// validation lands in Phase 4.
	AgentIDHeader = "X-Agent-ID"

	// AgentIDMetadataKey is the gRPC metadata equivalent of
	// [AgentIDHeader]. gRPC normalises keys to lowercase.
	AgentIDMetadataKey = "x-agent-id"
)

// RESTRateLimitMiddleware enforces per-agent rate limits and circuit-
// breaker quarantine on inbound HTTP requests. Calls without an
// `X-Agent-ID` header are bucketed against the limiter's
// `UnauthenticatedID` (so a malicious caller cannot bypass the limiter
// by omitting the header) and emit `rate_limit.unauthenticated_caller`
// security-class audit events via the limiter.
//
// Returns 429 with `Retry-After: <window_seconds>` on rate-limit deny,
// 403 with `agent quarantined` body on circuit-breaker deny.
//
// The middleware is intentionally additive: `nil` limiter or breaker
// is a passthrough. Callers wiring this from cmd/orchestrator must
// construct both in main.go before mounting.
func RESTRateLimitMiddleware(limiter *RateLimiter, breaker *CircuitBreaker) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		if limiter == nil && breaker == nil {
			return next
		}
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			agentID := r.Header.Get(AgentIDHeader)
			if breaker != nil && agentID != "" && breaker.IsQuarantined(agentID) {
				writeJSONError(w, http.StatusForbidden, "QUARANTINED", "agent is quarantined")
				return
			}
			if limiter != nil && !limiter.Allow(agentID) {
				if breaker != nil && agentID != "" {
					breaker.RecordViolation(agentID, ViolationRateLimit)
				}
				w.Header().Set("Retry-After", strconv.Itoa(limiter.cfg.WindowSeconds))
				writeJSONError(w, http.StatusTooManyRequests, "RATE_LIMITED", "rate limit exceeded")
				return
			}
			next.ServeHTTP(w, r)
		})
	}
}

// GRPCRateLimitInterceptor mirrors [RESTRateLimitMiddleware] for the
// gRPC server. Maps deny outcomes to the canonical gRPC status codes:
// `PermissionDenied` for quarantine, `ResourceExhausted` for rate
// limit (so clients can distinguish a transient back-off from a
// terminal break).
//
// On rate-limit deny the interceptor sets a `retry-after-seconds`
// gRPC trailer (PR #244 review M-04) so clients have parity with the
// REST `Retry-After` header and can back off intelligently.
//
// NOTE: only unary RPCs are covered. A streaming interceptor is not
// wired here; if a streaming RPC is added before Phase 4 it will
// bypass the limiter. Tracked as PR #244 review NTH-01.
func GRPCRateLimitInterceptor(limiter *RateLimiter, breaker *CircuitBreaker) grpc.UnaryServerInterceptor {
	return func(ctx context.Context, req any, info *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (any, error) {
		agentID := agentIDFromIncoming(ctx)
		if breaker != nil && agentID != "" && breaker.IsQuarantined(agentID) {
			return nil, status.Error(codes.PermissionDenied, "agent is quarantined")
		}
		if limiter != nil && !limiter.Allow(agentID) {
			if breaker != nil && agentID != "" {
				breaker.RecordViolation(agentID, ViolationRateLimit)
			}
			_ = grpc.SetHeader(ctx, metadata.Pairs(
				"retry-after-seconds", strconv.Itoa(limiter.cfg.WindowSeconds),
			))
			return nil, status.Error(codes.ResourceExhausted, "rate limit exceeded")
		}
		return handler(ctx, req)
	}
}

func agentIDFromIncoming(ctx context.Context) string {
	md, ok := metadata.FromIncomingContext(ctx)
	if !ok {
		return ""
	}
	if vals := md.Get(AgentIDMetadataKey); len(vals) > 0 {
		return vals[0]
	}
	return ""
}

func writeJSONError(w http.ResponseWriter, status int, code, msg string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]any{
		"error": map[string]string{
			"code":    code,
			"message": msg,
		},
	})
}
