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
	RunID      string         `json:"run_id"`
	WorkflowID string         `json:"workflow_id"`
	Status     string         `json:"status"`
	Error      string         `json:"error,omitempty"` // Non-empty when Status == "failed" (N-23)
	StartedAt  *time.Time     `json:"started_at"`      // *time.Time → null when zero (M-07)
	FinishedAt *time.Time     `json:"finished_at"`     // *time.Time → null when zero (M-07)
	Steps      map[string]any `json:"steps"`
}

// registerAgentRequest is the JSON request body for POST /api/v1/agents/register.
type registerAgentRequest struct {
	ID           string   `json:"id"`
	Address      string   `json:"address"`
	Capabilities []string `json:"capabilities"`
}

// agentResponse is the JSON response for agent endpoints.
// registry.AgentInfo has no json tags and would produce PascalCase JSON if
// serialized directly — these snake_case tags match the workflow DTO convention (F-15).
type agentResponse struct {
	ID           string   `json:"id"`
	Address      string   `json:"address"`
	Capabilities []string `json:"capabilities"`
	Status       string   `json:"status"`
}

// errorResponse is the standard JSON error envelope.
type errorResponse struct {
	Error string `json:"error"`
	Code  string `json:"code"`
}
