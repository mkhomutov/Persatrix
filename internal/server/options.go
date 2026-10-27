package server

// Most of the options New takes, each handing the server an optional
// dependency. The rest live beside the routes they serve: WithAuth in
// auth_middleware.go, WithInteractionReader in interactions_handler.go, and
// WithUI and WithUIConfig in ui.go. Split out of server.go, at its 500-line
// cap, when the society store's option joined them (RFC 0029 Phase 3 plan
// PR 4).

import (
	"net/http"

	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/channels"
	"github.com/mkhomutov/persatrix/internal/cost"
	"github.com/mkhomutov/persatrix/internal/executor"
	"github.com/mkhomutov/persatrix/internal/observability/logbuffer"
	obsmetrics "github.com/mkhomutov/persatrix/internal/observability/metrics"
	"github.com/mkhomutov/persatrix/internal/security"
	"github.com/mkhomutov/persatrix/internal/society"
)

// ServerOption configures optional Server dependencies.
type ServerOption func(*Server)

// WithCostReporter injects a CostReporter for the cost summary endpoint.
func WithCostReporter(reporter *cost.CostReporter) ServerOption {
	return func(s *Server) {
		s.costReporter = reporter
	}
}

// WithChatExecutor injects a ChatExecutor for the chat endpoint.
func WithChatExecutor(ce executor.ChatExecutor) ServerOption {
	return func(s *Server) {
		s.chatExecutor = ce
	}
}

// WithHandlerWrapper sets a middleware that wraps the composed HTTP handler.
// Applied as the outermost layer in Handler() — useful for OTEL HTTP tracing.
func WithHandlerWrapper(wrapper func(http.Handler) http.Handler) ServerOption {
	return func(s *Server) {
		s.handlerWrapper = wrapper
	}
}

// WithMetrics injects the orchestrator metric instruments used by the
// workflow-submit handler (orchestrator.workflow.submitted counter).
// Nil-safe: when unset, no metrics are recorded.
func WithMetrics(inst *obsmetrics.Instruments) ServerOption {
	return func(s *Server) {
		s.metrics = inst
	}
}

// WithLogBuffer injects the orchestrator log buffer.  When unset the
// log REST + SSE endpoints return 501 NOT_IMPLEMENTED.  Wired in
// cmd/orchestrator/main.go alongside the gRPC LogService server.
func WithLogBuffer(buf *logbuffer.Buffer) ServerOption {
	return func(s *Server) {
		s.logBuffer = buf
	}
}

// WithAuditLogger injects the security audit logger used by the
// agent-registration handler (and future security-bearing endpoints) to
// emit structured audit events.  Nil-safe: when unset, emit sites no-op
// so callers that do not opt into audit retain their existing behaviour.
//
// Wired from cmd/orchestrator/main.go via OBSERVABILITY_AUDIT_PATH
// (RFC 0009 PR 1b).
func WithAuditLogger(a security.AuditLogger) ServerOption {
	return func(s *Server) {
		s.auditor = a
	}
}

// WithRateLimiter injects the per-agent REST rate limiter and circuit
// breaker (RFC 0009 PR 2). Both are applied as middleware on the public
// REST router so denials short-circuit before any handler-side work.
// Nil-safe: passing nil for either degrades that subsystem to a
// passthrough (used by unit tests that do not exercise the limiter).
func WithRateLimiter(rl *security.RateLimiter, cb *security.CircuitBreaker) ServerOption {
	return func(s *Server) {
		s.rateLimiter = rl
		s.circuitBreaker = cb
	}
}

// WithUnquarantineToken injects an optional shared secret that gates the
// POST /api/v1/agents/{id}/unquarantine endpoint (PR #244 review H-02).
//
// The endpoint undoes a security control (a circuit-breaker quarantine)
// and is otherwise unauthenticated until token-based auth lands in
// RFC 0009 Phase 4. Operators who cannot front the orchestrator with an
// authenticating reverse proxy can opt into a defense-in-depth check
// here by setting `SECURITY_UNQUARANTINE_TOKEN`; the bootstrap reads
// the env var and applies this option in cmd/orchestrator.
//
// Empty token disables the check (preserves pre-PR-244 behaviour) so
// minimal deployments and unit tests need not opt in.
func WithUnquarantineToken(token string) ServerOption {
	return func(s *Server) {
		s.unquarantineToken = token
	}
}

// WithChannels injects the channel store and (optionally) the channel
// router used by the RFC 0011 §C REST endpoints. Pass nil for the
// router when the caller only wants direct-store reads (history/list)
// without publish-side fanout — the publish handler then writes through
// the store directly. Production callers always wire both.
func WithChannels(store channels.ChannelStore, router *channels.ChannelRouter) ServerOption {
	return func(s *Server) {
		s.channelStore = store
		s.channelRouter = router
		// RFC 0031 Phase 3 §E: the session registry rides on the same store,
		// so wire it wherever channels are wired. Construction only fails on a
		// programming error (a non-SQLite store); degrade to nil (the
		// /api/v1/sessions endpoints return 503) rather than failing channel
		// wiring — the registry is additive to the existing channel surface.
		reg, err := channels.NewSessionRegistry(store)
		if err != nil {
			s.logger.Warn("channels: session registry unavailable; /api/v1/sessions will return 503", zap.Error(err))
			return
		}
		s.sessionRegistry = reg
	}
}

// WithChannelSessionID injects the per-process default session id stamped
// on every CreateChannel / PublishMessage that arrives without an explicit
// session_id (RFC 0031 Phase 1). Sourced from PERSATRIX_SESSION_ID at
// orchestrator boot. Empty values are accepted so the store-side `legacy`
// default applies for tests and minimal deployments that do not opt in.
func WithChannelSessionID(sessionID string) ServerOption {
	return func(s *Server) {
		s.channelSessionID = sessionID
	}
}

// WithSociety hands the server the society store, which startup builds on
// the channels store's connection (RFC 0029 Phase 3). Nothing on the server
// reads it yet: RFC 0009's approval-queue routes will be its first reader.
// With channels off, or a store that could not be built, the option is not
// passed.
func WithSociety(st society.Store) ServerOption {
	return func(s *Server) {
		s.societyStore = st
	}
}

// WithUI (RFC 0048 web console) lives in ui.go alongside the /ui/ route
// registration, keeping all console wiring co-located.
