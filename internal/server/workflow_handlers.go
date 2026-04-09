package server

import (
	"errors"
	"net/http"
	"path/filepath"
	"strings"
	"time"

	"go.uber.org/zap"

	"github.com/orchestr8/orchestr8/internal/planner"
	"github.com/orchestr8/orchestr8/internal/state"
)

// workflowIDRegex is imported from the planner package to ensure a single source
// of truth for the workflow ID validation pattern across security boundaries.
// (Review finding F-04: eliminates divergence risk between planner and server.)
var workflowIDRegex = planner.WorkflowIDRegex

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

	if req.WorkflowID == "" {
		writeError(w, "BAD_REQUEST", "workflow_id is required", http.StatusBadRequest)
		return
	}
	if !workflowIDRegex.MatchString(req.WorkflowID) {
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
	if !workflowIDRegex.MatchString(workflowID) {
		return "", ErrInvalidWorkflowID
	}

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
		// TODO(v0.3): populate from run.Steps when Scheduler/Executor is implemented (RFC 0003)
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
	default:
		return "unknown"
	}
}
