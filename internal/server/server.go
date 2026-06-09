// Package server implements the HTTP/REST API server for the Persatrix orchestrator.
package server

import (
	"context"
	"errors"
	"fmt"
	"io/fs"
	"net/http"
	"os"
	"path/filepath"
	"time"

	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/channels"
	"github.com/mkhomutov/persatrix/internal/cost"
	"github.com/mkhomutov/persatrix/internal/executor"
	"github.com/mkhomutov/persatrix/internal/observability/logbuffer"
	obsmetrics "github.com/mkhomutov/persatrix/internal/observability/metrics"
	"github.com/mkhomutov/persatrix/internal/planner"
	"github.com/mkhomutov/persatrix/internal/registry"
	"github.com/mkhomutov/persatrix/internal/security"
	"github.com/mkhomutov/persatrix/internal/state"
)

// Server is the HTTP/REST API server for the orchestrator.
type Server struct {
	addr         string
	workflowsDir string // canonicalized absolute path to workflow YAML directory
	store        state.Store
	registry     registry.Registry
	planner      planner.Planner
	logger       *zap.Logger
	mux          *http.ServeMux

	// Cost components (optional — nil when cost tracking is not configured).
	costReporter *cost.CostReporter

	// Optional, nil-safe: chatExecutor dispatches chat; interactionReader
	// (v0.3.8) reads closed-interaction summaries (nil → 503).
	chatExecutor      executor.ChatExecutor
	interactionReader executor.InteractionReader

	// handlerWrapper optionally wraps the composed HTTP handler (e.g. otelhttp).
	handlerWrapper func(http.Handler) http.Handler

	// Metrics instruments (optional — nil-safe).  Wired in RFC 0019 PR 3.
	metrics *obsmetrics.Instruments

	// Log buffer (optional — nil-safe).  Wired in RFC 0018 PR 5.
	// When unset, the log REST + SSE endpoints return 501 NOT_IMPLEMENTED.
	logBuffer *logbuffer.Buffer

	// Audit logger (optional — nil-safe).  Wired in RFC 0009 PR 1b
	// (security audit log).  When nil, audit emit sites no-op so unit
	// tests and minimal-deployment fixtures stay zero-config.
	auditor security.AuditLogger

	// Rate limiter + circuit breaker (optional — nil-safe). Wired in
	// RFC 0009 PR 2. When nil, the rate-limit middleware degrades to a
	// passthrough so existing unit tests and minimal deployments keep
	// their pre-PR behaviour.
	rateLimiter    *security.RateLimiter
	circuitBreaker *security.CircuitBreaker

	// unquarantineToken is the optional shared-secret stop-gap gating the
	// operator-only unquarantine endpoint until token-based auth lands in
	// RFC 0009 Phase 4 (PR #244 review H-02). Empty → unauthenticated
	// (pre-PR-244 "front with an authenticating reverse proxy" posture); set
	// → callers present `Authorization: Bearer <token>` (crypto/subtle compare).
	unquarantineToken string

	// Channel components (RFC 0011 PR 2 — optional, nil-safe).
	// channelStore is the persistence boundary; channelRouter wraps it
	// with publish-side fanout + channel_type cross-validation. When
	// either is nil, the channel REST endpoints return 503 UNAVAILABLE
	// — preserves zero-config behaviour for unit tests and for
	// deployments that have not opted into the channels subsystem.
	channelStore  channels.ChannelStore
	channelRouter *channels.ChannelRouter

	// sessionRegistry is the operator-facing CRUD surface over the
	// `sessions` table (RFC 0031 Phase 3 §E), built from the channel store
	// in WithChannels. When nil — channels not wired, or a non-SQLite store
	// — the /api/v1/sessions endpoints return 503 UNAVAILABLE, matching the
	// channel handlers' zero-config degradation.
	sessionRegistry *channels.SessionRegistry

	// channelSessionID is the per-process default session id stamped on
	// every CreateChannel / PublishMessage that arrives without an
	// explicit session_id (RFC 0031 Phase 1). Sourced from
	// PERSATRIX_SESSION_ID at orchestrator boot. An empty value here
	// lets the store boundary apply its own `legacy` default — the two
	// defaults are intentionally co-located in case a future
	// session-aware handler shape (Phase 3 `--session` flag) needs to
	// distinguish "no value supplied" from "operator picked legacy".
	channelSessionID string

	// uiFS is the embedded web-console asset tree (RFC 0048 Phase 1 PR 1 —
	// optional, nil-safe). When non-nil, registerRoutes serves it under
	// /ui/ via a static file server; when nil — the default, and whenever
	// --enable-ui is off — the /ui/ route is never registered, so /ui/ is a
	// clean 404 and the rest of the surface is untouched. Wired via WithUI.
	uiFS fs.FS
	// uiConfig holds the parsed config/ui.yaml feature toggles (RFC 0048 PR 2)
	// reported by /api/v1/ui/config; nil → the Slice-1 defaults. The companion
	// `available` flag is runtime-derived (Server.panelAvailable), not stored.
	uiConfig *UIConfig
}

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

// WithUI (RFC 0048 web console) lives in ui.go alongside the /ui/ route
// registration, keeping all console wiring co-located.

// New validates that workflowsDir is accessible and returns a configured Server.
// Returns an error if the workflows directory is missing, inaccessible, or not a directory.
func New(addr, workflowsDir string, store state.Store, reg registry.Registry, pl planner.Planner, logger *zap.Logger, opts ...ServerOption) (*Server, error) {
	if logger == nil {
		logger = zap.NewNop()
	}

	fi, err := os.Stat(workflowsDir)
	if err != nil {
		return nil, fmt.Errorf("workflows directory %q not accessible: %w", workflowsDir, err)
	}
	if !fi.IsDir() {
		return nil, fmt.Errorf("workflows directory %q is not a directory", workflowsDir)
	}

	// Canonicalize once at startup: resolve to absolute path and follow symlinks.
	// This avoids repeated filepath.EvalSymlinks syscalls on every request in
	// resolveWorkflowPath, and eliminates a theoretical correctness issue where
	// a relative workflowsDir could resolve differently if the process cwd
	// changed between requests. (Review finding CS-02)
	absDir, err := filepath.Abs(workflowsDir)
	if err != nil {
		return nil, fmt.Errorf("workflows directory %q: failed to resolve absolute path: %w", workflowsDir, err)
	}
	canonicalDir, err := filepath.EvalSymlinks(absDir)
	if err != nil {
		return nil, fmt.Errorf("workflows directory %q: failed to resolve symlinks: %w", workflowsDir, err)
	}

	s := &Server{
		addr:         addr,
		workflowsDir: canonicalDir,
		store:        store,
		registry:     reg,
		planner:      pl,
		logger:       logger,
		mux:          http.NewServeMux(),
	}
	for _, opt := range opts {
		opt(s)
	}

	// RFC 0031 Phase 3 PR 4: the boot-default session id (PERSATRIX_SESSION_ID,
	// via WithChannelSessionID) rides the same gRPC `persatrix-session` metadata
	// header as a per-request `--session` override, so it must satisfy the same
	// wire-legality (printable ASCII). A control / non-ASCII byte here would make
	// *every* channel dispatch fail at gRPC send time — worse than the graceful
	// legacy fallback, and not surfaced until the first message silently drops.
	// Fail loud at construction instead, reusing the override charset check. An
	// empty id passes (the store applies its own `legacy` default).
	if !sessionOverrideValid(s.channelSessionID) {
		return nil, fmt.Errorf(
			"channel session id (PERSATRIX_SESSION_ID) must be printable ASCII (no control or non-ASCII characters)")
	}

	s.registerRoutes()
	return s, nil
}

// registerRoutes sets up all HTTP routes on the server's mux.
func (s *Server) registerRoutes() {
	// TODO(security): no auth in v0.1
	// TODO(v0.2): rename /api/v1/workflows to /api/v1/workflows/runs when definition endpoints are added
	// TODO(spec-sync): update ai-agents-orchestration-spec.md §8.3 to include GET /api/v1/workflows and DELETE /api/v1/workflows/{id}

	// Workflow run endpoints (Phase 1)
	s.mux.HandleFunc("POST /api/v1/workflows/run", s.handleSubmitWorkflowRun)
	s.mux.HandleFunc("GET /api/v1/workflows/{id}/status", s.handleGetWorkflowStatus)
	s.mux.HandleFunc("GET /api/v1/workflows", s.handleListWorkflows)
	s.mux.HandleFunc("DELETE /api/v1/workflows/{id}", s.handleDeleteWorkflow)

	// Agent registry endpoints (Phase 2)
	s.mux.HandleFunc("POST /api/v1/agents/register", s.handleRegisterAgent)
	s.mux.HandleFunc("GET /api/v1/agents", s.handleListAgents)
	s.mux.HandleFunc("GET /api/v1/agents/{id}", s.handleGetAgent)
	s.mux.HandleFunc("DELETE /api/v1/agents/{id}", s.handleDeleteAgent)

	// Operator endpoint — release an agent quarantined by the circuit
	// breaker (RFC 0009 PR 2). Returns 503 when no breaker is wired.
	s.mux.HandleFunc("POST /api/v1/agents/{id}/unquarantine", s.handleUnquarantineAgent)

	// Stub endpoints — deferred to future RFCs (Phase 3)
	s.mux.HandleFunc("GET /api/v1/executions/{id}/logs", s.handleListLogs)
	s.mux.HandleFunc("GET /api/v1/executions/{id}/logs/stream", s.handleStreamLogs)

	// Cost summary endpoint (RFC 0006 PR 4b)
	s.mux.HandleFunc("GET /api/v1/cost/summary", s.handleGetCostSummaryImpl)

	// Chat endpoints (RFC 0016 PR 4; history GET = RFC 0048 §B web-console resume).
	// TODO(v0.2): per-IP/per-session rate limiting — chat is unauthenticated with no rate controls beyond the 300s timeout cap.
	s.mux.HandleFunc("POST /api/v1/agents/{id}/chat", s.handleChat)
	s.mux.HandleFunc("GET /api/v1/agents/{id}/chat/history", s.handleGetChatHistory)
	s.mux.HandleFunc("GET /api/v1/agents/{id}/interactions/closed", s.handleGetClosedInteractions)

	// Channels endpoints (RFC 0011 §C). DELETE handlers landed in PR 4b
	// alongside the response gate; full §C surface is now implemented.
	s.mux.HandleFunc("POST /api/v1/channels", s.handleCreateChannel)
	s.mux.HandleFunc("GET /api/v1/channels", s.handleListChannels)
	s.mux.HandleFunc("GET /api/v1/channels/{id}", s.handleGetChannel)
	s.mux.HandleFunc("DELETE /api/v1/channels/{id}", s.handleDeleteChannel)
	s.mux.HandleFunc("POST /api/v1/channels/{id}/messages", s.handlePublishMessage)
	s.mux.HandleFunc("GET /api/v1/channels/{id}/messages", s.handleGetChannelHistory)
	s.mux.HandleFunc("GET /api/v1/channels/{id}/messages/{msg_id}/thread", s.handleGetThread)
	s.mux.HandleFunc("POST /api/v1/channels/{id}/members", s.handleAddChannelMember)
	s.mux.HandleFunc("DELETE /api/v1/channels/{id}/members/{participant_id}", s.handleDeleteChannelMember)

	// Session registry endpoints (RFC 0031 Phase 3 §E operator surface).
	// Enabler for the `persatrix session …` CLI verbs (PR 2); return 503
	// when the channels subsystem (and thus the registry) is not wired.
	s.mux.HandleFunc("POST /api/v1/sessions", s.handleCreateSession)
	s.mux.HandleFunc("GET /api/v1/sessions", s.handleListSessions)
	s.mux.HandleFunc("GET /api/v1/sessions/{id}", s.handleGetSession)
	s.mux.HandleFunc("POST /api/v1/sessions/{id}/archive", s.handleArchiveSession)

	// Embedded web console (RFC 0048) registers in Handler() on the
	// security-bypass root mux, not here — see registerUIRoutes.

	// Minimal health endpoint (C-02: satisfies existing docker-compose.yaml healthcheck)
	s.mux.HandleFunc("GET /healthz", s.handleHealthz)

	// NOTE: 405/404 from ServeMux are plain text (see RFC 0002 I-02)
}

// Handler returns the composed HTTP handler with middleware applied.
// Execution order (outermost first):
//
//	handlerWrapper (optional, e.g. otelhttp) → recovery → requestID →
//	logging → routeMux → (rate-limit + circuit-breaker for /api/v1/*) → mux
//
// requestID must run before logging so the request ID is present in r.Context()
// when the logging middleware reads it after next.ServeHTTP returns.
// (Review finding F-01: r.WithContext creates a new *http.Request, so
// loggingMiddleware must receive the request *after* requestID injects the ID.)
//
// The rate-limit/circuit-breaker layer is mounted **only on `/api/v1/*`**
// (PR #244 round-2 review H-03). Mounting it on the root would catch
// `/healthz`, which Kubernetes liveness probes call without an
// `X-Agent-ID` header — combined with the H-01 anonymous-deny that
// fires while a quarantine is active, a single quarantined agent would
// 403 the probe → restart the pod → drop the in-memory quarantine state
// → re-quarantine on the next request → crashloop.
//
// `/healthz` therefore bypasses the limiter+breaker (rate limiting a
// liveness probe is meaningless anyway). All other routes — including
// future top-level endpoints — must be added under `/api/v1/` or they
// will silently bypass the security middleware.
//
// The embedded web console (RFC 0048, WithUI) is the one deliberate exception:
// it registers on this root mux too, so its anonymous operator traffic also
// bypasses the limiter+breaker — leaving it under the H-01 deny would 403 the
// console whenever an agent is quarantined. See registerUIRoutes.
//
// The 429/403 short-circuit responses still flow back through
// loggingMiddleware and appear in the access log with their
// X-Request-ID header set, because both wrappers sit inside the logging
// + requestID layers (PR #244 review L-04/L-05).
func (s *Server) Handler() http.Handler {
	// TODO(v0.2): per-request timeout middleware — see RFC 0002 H3
	//
	// Path-scoped middleware mount: an outer mux splits /healthz (and the
	// RFC 0048 console) off to bypass the limiter+breaker while everything
	// else flows through the wrapped s.mux. The /healthz copy on s.mux is
	// shadowed here but kept so direct-s.mux test/integration code still works.
	var apiH http.Handler = s.mux
	if s.rateLimiter != nil || s.circuitBreaker != nil {
		apiH = security.RESTRateLimitMiddleware(s.rateLimiter, s.circuitBreaker)(apiH)
	}
	root := http.NewServeMux()
	root.HandleFunc("GET /healthz", s.handleHealthz)
	s.registerUIRoutes(root) // RFC 0048 console: bypass limiter+breaker like /healthz (no-op when WithUI unwired)
	root.Handle("/", apiH)

	var h http.Handler = root
	h = loggingMiddleware(s.logger, h)
	h = requestIDMiddleware(h)
	h = recoveryMiddleware(s.logger, h)
	if s.handlerWrapper != nil {
		h = s.handlerWrapper(h)
	}
	return h
}

// emitAudit forwards ev to the configured AuditLogger if one was injected.
// Nil-safe: when no auditor is wired, the call no-ops so handler code can
// stay free of conditional guards.
//
// Context handling (PR #234 review M-1): callers pass `r.Context()` so
// trace/correlation values propagate to the sink, but the parent context
// is canceled the instant the HTTP client disconnects. For post-success
// audit emits (e.g. `agent.registered` after the registry write commits)
// that cancellation would silently drop the only forensic record of the
// completed side effect. We detach cancellation here via
// [context.WithoutCancel] so values still propagate but Emit's
// `ctx.Err()` short-circuit cannot fire mid-flush. Deadlines are
// intentionally also stripped: a stalled sink is its own incident
// surfaced via the audit_emit_latency_seconds histogram (PR 1c), not
// something to silently drop.
//
// Emit errors are logged at debug level for telemetry-class events
// (audit emission must never block the orchestrator's user-facing
// response) and at warn for security-class events, where a write failure
// means the tamper-evident chain just broke and an operator needs to
// know (PR #234 review L-6).
func (s *Server) emitAudit(ctx context.Context, ev security.AuditEvent) {
	if s.auditor == nil {
		return
	}
	emitCtx := context.WithoutCancel(ctx)
	if err := s.auditor.Emit(emitCtx, ev); err != nil {
		fields := []zap.Field{
			zap.String("event_type", string(ev.EventType)),
			zap.String("agent_id", ev.AgentID),
			zap.Error(err),
		}
		if security.IsSecurityEvent(ev.EventType) {
			s.logger.Warn("audit emit failed (security-class)", fields...)
		} else {
			s.logger.Debug("audit emit failed", fields...)
		}
	}
}

// Start runs the HTTP server until the context is cancelled, then drains
// with a 10-second graceful shutdown window.
// (Review finding F-05: previous implementation leaked a goroutine when
// ListenAndServe failed immediately, because the shutdown goroutine blocked
// on <-ctx.Done() forever. This select-based approach ensures no leak.)
func (s *Server) Start(ctx context.Context) error {
	srv := &http.Server{
		Addr:    s.addr,
		Handler: s.Handler(),
		// Transport-level timeouts to mitigate Slow Loris and idle-connection
		// resource exhaustion (gosec G112). These are independent of RFC 0002 H3's
		// per-request handler timeouts (deferred to v0.2).
		// WriteTimeout is intentionally omitted — it would break v0.2 SSE streaming;
		// per-handler write deadlines should use http.ResponseController instead.
		ReadHeaderTimeout: 10 * time.Second,
		ReadTimeout:       30 * time.Second,
		IdleTimeout:       120 * time.Second,
	}
	errCh := make(chan error, 1)
	go func() {
		errCh <- srv.ListenAndServe()
	}()
	select {
	case err := <-errCh:
		if err != nil && !errors.Is(err, http.ErrServerClosed) {
			return err
		}
		return nil
	case <-ctx.Done():
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		if err := srv.Shutdown(shutdownCtx); err != nil {
			s.logger.Error("HTTP server shutdown error", zap.Error(err))
		}
		// Wait for ListenAndServe to return after Shutdown.
		<-errCh
		return nil
	}
}

// handleHealthz returns a minimal 200 OK with {"status": "ok"}.
func (s *Server) handleHealthz(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, map[string]string{"status": "ok"}, http.StatusOK)
}
