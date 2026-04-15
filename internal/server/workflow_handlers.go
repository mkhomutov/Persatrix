package server

import (
	"errors"
	"net/http"
	"path/filepath"
	"strings"
	"time"

	"go.uber.org/zap"

	"github.com/persatrix/persatrix/internal/planner"
	"github.com/persatrix/persatrix/internal/state"
)

// resourceIDRegex is imported from the planner package to ensure a single source
// of truth for the resource ID validation pattern across security boundaries.
// Used for both workflow IDs and agent IDs (renamed from workflowIDRegex per PR #16 F-04).
var resourceIDRegex = planner.ResourceIDRegex

// Sentinel errors for workflow path resolution.
var (
	ErrInvalidWorkflowID = errors.New("invalid workflow ID")
	ErrWorkflowNotFound  = errors.New("workflow not found")
)

// handleSubmitWorkflowRun handles POST /api/v1/workflows/run.
func (s *Server) handleSubmitWorkflowRun(w http.ResponseWriter, r *http.Request) {
	if !requireJSON(w, r) {
		return
	}
	var req submitWorkflowRunRequest
	if !decodeJSON(w, r, &req) {
		return
	}

	// TODO(v0.3): validate input key names against variable name charset [a-z_][a-z0-9_]*
	if req.WorkflowID == "" {
		writeError(w, "BAD_REQUEST", "workflow_id is required", http.StatusBadRequest)
		return
	}
	if !resourceIDRegex.MatchString(req.WorkflowID) {
		writeError(w, "BAD_REQUEST", "workflow_id must match ^[a-z0-9][a-z0-9-]*[a-z0-9]$", http.StatusBadRequest)
		return
	}

	resolvedPath, err := s.resolveWorkflowPath(req.WorkflowID)
	if err != nil {
		// Defense-in-depth (Review finding F-12): this branch is currently unreachable
		// because the regex check at the top of the handler already rejects invalid IDs.
		// Kept as a safety net in case resolveWorkflowPath is called from a new code path
		// that doesn't pre-validate the ID.
		if errors.Is(err, ErrInvalidWorkflowID) {
			writeError(w, "BAD_REQUEST", "invalid workflow_id format", http.StatusBadRequest)
			return
		}
		writeError(w, "NOT_FOUND", "workflow not found", http.StatusNotFound)
		return
	}

	wf, err := s.planner.Parse(r.Context(), resolvedPath)
	if err != nil {
		// (Review finding F-10): Log the full error server-side but return a generic
		// message to prevent leaking filesystem paths or YAML parser internals.
		s.logger.Warn("workflow parse failed",
			zap.String("workflow_id", req.WorkflowID), zap.Error(err))
		writeError(w, "UNPROCESSABLE", "workflow file could not be parsed", http.StatusUnprocessableEntity)
		return
	}

	if err := s.planner.ValidateDAG(r.Context(), wf); err != nil {
		// (Review finding F-11): Log the full error server-side but return a generic
		// message to prevent leaking internal step IDs and dependency structure.
		s.logger.Warn("workflow DAG validation failed",
			zap.String("workflow_id", req.WorkflowID), zap.Error(err))
		writeError(w, "UNPROCESSABLE", "workflow contains invalid dependencies", http.StatusUnprocessableEntity)
		return
	}

	// Note (I-03): StartedAt is set at submission time ("submitted at" semantics).
	// A future RFC should add CreatedAt and reset StartedAt to zero until Running.
	now := time.Now()
	run := &state.WorkflowRun{
		WorkflowID: req.WorkflowID,
		Status:     state.RunPending,
		StartedAt:  now,
		Inputs:     req.Inputs,
	}

	if err := s.store.CreateRun(r.Context(), run); err != nil {
		s.logger.Error("failed to create workflow run", zap.Error(err))
		writeError(w, "INTERNAL", "failed to create workflow run", http.StatusInternalServerError)
		return
	}

	writeJSON(w, submitWorkflowRunResponse{
		RunID:      run.ID,
		WorkflowID: run.WorkflowID,
		Status:     "pending",
	}, http.StatusCreated)
}

// handleGetWorkflowStatus handles GET /api/v1/workflows/{id}/status.
func (s *Server) handleGetWorkflowStatus(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if !validateRunID(w, id) {
		return
	}
	run, err := s.store.GetRun(r.Context(), id)
	if err != nil {
		if errors.Is(err, state.ErrRunNotFound) {
			writeError(w, "NOT_FOUND", "run not found", http.StatusNotFound)
			return
		}
		s.logger.Error("failed to get workflow run", zap.Error(err))
		writeError(w, "INTERNAL", "failed to get workflow run", http.StatusInternalServerError)
		return
	}

	writeJSON(w, runToResponse(run), http.StatusOK)
}

// handleListWorkflows handles GET /api/v1/workflows.
func (s *Server) handleListWorkflows(w http.ResponseWriter, r *http.Request) {
	// TODO(v0.2): add pagination
	runs, err := s.store.ListRuns(r.Context())
	if err != nil {
		s.logger.Error("failed to list workflow runs", zap.Error(err))
		writeError(w, "INTERNAL", "failed to list workflow runs", http.StatusInternalServerError)
		return
	}

	resp := make([]workflowRunResponse, 0, len(runs))
	for _, run := range runs {
		resp = append(resp, runToResponse(run))
	}
	writeJSON(w, resp, http.StatusOK)
}

// handleDeleteWorkflow handles DELETE /api/v1/workflows/{id}.
func (s *Server) handleDeleteWorkflow(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if !validateRunID(w, id) {
		return
	}

	run, err := s.store.GetRun(r.Context(), id)
	if err != nil {
		if errors.Is(err, state.ErrRunNotFound) {
			writeError(w, "NOT_FOUND", "run not found", http.StatusNotFound)
			return
		}
		s.logger.Error("failed to get workflow run for delete", zap.Error(err))
		writeError(w, "INTERNAL", "failed to get workflow run", http.StatusInternalServerError)
		return
	}

	if run.Status == state.RunRunning {
		writeError(w, "CONFLICT", "cannot delete a running workflow run", http.StatusConflict)
		return
	}

	// TODO(v0.3): atomic check-and-delete or store-level status guard
	// NOTE (Review finding F-01): TOCTOU race — if a concurrent request deletes the
	// run between GetRun and DeleteRun, DeleteRun returns ErrRunNotFound and the
	// client receives 404 despite having confirmed the run existed. This is
	// acceptable in v0.1: 404 is semantically correct (run no longer exists),
	// and the race is dormant with a single-server in-memory store.
	if err := s.store.DeleteRun(r.Context(), id); err != nil {
		if errors.Is(err, state.ErrRunNotFound) {
			writeError(w, "NOT_FOUND", "run not found", http.StatusNotFound)
			return
		}
		s.logger.Error("failed to delete workflow run", zap.Error(err))
		writeError(w, "INTERNAL", "failed to delete workflow run", http.StatusInternalServerError)
		return
	}

	w.WriteHeader(http.StatusNoContent)
}

// resolveWorkflowPath maps a validated workflow ID to a canonical filesystem path
// within the workflows directory. Returns ErrWorkflowNotFound for traversal attempts
// or missing files (no information leakage about path structure).
func (s *Server) resolveWorkflowPath(workflowID string) (string, error) {
	if !resourceIDRegex.MatchString(workflowID) {
		return "", ErrInvalidWorkflowID
	}

	// Only .yaml extension is supported (consistent with project convention).
	candidate := filepath.Join(s.workflowsDir, workflowID+".yaml")

	resolved, err := filepath.EvalSymlinks(candidate)
	if err != nil {
		s.logger.Debug("EvalSymlinks failed", zap.String("workflow_id", workflowID), zap.Error(err))
		return "", ErrWorkflowNotFound
	}

	if !strings.HasPrefix(resolved, s.workflowsDir+string(filepath.Separator)) {
		return "", ErrWorkflowNotFound
	}

	return resolved, nil
}

// runToResponse converts a domain WorkflowRun to a wire-format response DTO.
func runToResponse(run *state.WorkflowRun) workflowRunResponse {
	resp := workflowRunResponse{
		RunID:      run.ID,
		WorkflowID: run.WorkflowID,
		Status:     runStatusString(run.Status),
		Error:      run.Error,
		// TODO(v0.2): populate from run.Steps — step state data is now written by
		// the Scheduler (RFC 0003), but the wire format for per-step status/output
		// needs to be defined before exposing it.
		Steps: make(map[string]any),
	}

	if !run.StartedAt.IsZero() {
		t := run.StartedAt.UTC()
		resp.StartedAt = &t
	}
	if !run.FinishedAt.IsZero() {
		t := run.FinishedAt.UTC()
		resp.FinishedAt = &t
	}

	return resp
}

// runStatusString maps RunStatus to lowercase JSON wire-format strings.
func runStatusString(s state.RunStatus) string {
	switch s {
	case state.RunPending:
		return "pending"
	case state.RunRunning:
		return "running"
	case state.RunCompleted:
		return "completed"
	case state.RunFailed:
		return "failed"
	case state.RunCancelled:
		return "cancelled"
	case state.RunRetrying:
		return "retrying"
	default:
		return "unknown"
	}
}
