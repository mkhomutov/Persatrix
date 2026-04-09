// Package server implements the HTTP/REST API server for the Orchestr8 orchestrator.
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

	"github.com/orchestr8/orchestr8/internal/planner"
	"github.com/orchestr8/orchestr8/internal/registry"
	"github.com/orchestr8/orchestr8/internal/state"
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
}

// New validates that workflowsDir is accessible and returns a configured Server.
// Returns an error if the workflows directory is missing, inaccessible, or not a directory.
func New(addr, workflowsDir string, store state.Store, reg registry.Registry, pl planner.Planner, logger *zap.Logger) (*Server, error) {
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
	s.mux.HandleFunc("GET /api/v1/executions/{id}/logs", s.handleGetLogs)
	s.mux.HandleFunc("GET /api/v1/cost/summary", s.handleGetCostSummary)

	// Minimal health endpoint (C-02: satisfies existing docker-compose.yaml healthcheck)
	s.mux.HandleFunc("GET /healthz", s.handleHealthz)

	// NOTE: 405/404 from ServeMux are plain text (see RFC 0002 I-02)
}

// Handler returns the composed HTTP handler with middleware applied.
// Execution order: recovery → requestID → logging → mux.
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
	return h
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
