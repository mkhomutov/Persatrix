package server

import "time"

// submitWorkflowRunRequest is the JSON request body for POST /api/v1/workflows/run.
type submitWorkflowRunRequest struct {
	WorkflowID string            `json:"workflow_id"`
	Inputs     map[string]string `json:"inputs"`
}

// submitWorkflowRunResponse is the JSON response for POST /api/v1/workflows/run.
type submitWorkflowRunResponse struct {
	RunID      string `json:"run_id"`
	WorkflowID string `json:"workflow_id"`
	Status     string `json:"status"`
}

// workflowRunResponse is the JSON response for GET /api/v1/workflows/{id}/status
// and each element in the list response.
type workflowRunResponse struct {
	RunID      string            `json:"run_id"`
	WorkflowID string           `json:"workflow_id"`
	Status     string            `json:"status"`
	StartedAt  *time.Time        `json:"started_at"`  // *time.Time → null when zero (M-07)
	FinishedAt *time.Time        `json:"finished_at"` // *time.Time → null when zero (M-07)
	Steps      map[string]any    `json:"steps"`
}

// errorResponse is the standard JSON error envelope.
type errorResponse struct {
	Error string `json:"error"`
	Code  string `json:"code"`
}
