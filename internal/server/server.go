// Package server implements the HTTP/REST API server for the Persatrix orchestrator.
package server

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"time"

	"go.uber.org/zap"

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

	// Chat components (optional — nil when chat dispatch is not configured).
	chatExecutor executor.ChatExecutor

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

	// Stub endpoints — deferred to future RFCs (Phase 3)
	s.mux.HandleFunc("GET /api/v1/executions/{id}/logs", s.handleListLogs)
	s.mux.HandleFunc("GET /api/v1/executions/{id}/logs/stream", s.handleStreamLogs)

	// Cost summary endpoint (RFC 0006 PR 4b)
	s.mux.HandleFunc("GET /api/v1/cost/summary", s.handleGetCostSummaryImpl)

	// Chat endpoint (RFC 0016 PR 4)
	// TODO(v0.2): per-IP or per-session rate limiting — chat accepts unauthenticated
	// traffic and has no request-rate controls beyond the 300s timeout cap.
	s.mux.HandleFunc("POST /api/v1/agents/{id}/chat", s.handleChat)

	// Minimal health endpoint (C-02: satisfies existing docker-compose.yaml healthcheck)
	s.mux.HandleFunc("GET /healthz", s.handleHealthz)

	// NOTE: 405/404 from ServeMux are plain text (see RFC 0002 I-02)
}

// Handler returns the composed HTTP handler with middleware applied.
// Execution order (outermost first): handlerWrapper (optional, e.g. otelhttp) →
// recovery → requestID → logging → mux.
// requestID must run before logging so the request ID is present in r.Context()
// when the logging middleware reads it after next.ServeHTTP returns.
// (Review finding F-01: r.WithContext creates a new *http.Request, so
// loggingMiddleware must receive the request *after* requestID injects the ID.)
func (s *Server) Handler() http.Handler {
	// TODO(v0.2): per-request timeout middleware — see RFC 0002 H3
	var h http.Handler = s.mux
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
