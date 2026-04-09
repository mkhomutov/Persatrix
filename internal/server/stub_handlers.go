package server

import "net/http"

// handleGetLogs handles GET /api/v1/executions/{id}/logs.
// Deferred to RFC 0003 — the Executor will capture step-level logs.
func (s *Server) handleGetLogs(w http.ResponseWriter, _ *http.Request) {
	writeError(w, "NOT_IMPLEMENTED", "not implemented in v0.1", http.StatusNotImplemented)
}

// handleGetCostSummary handles GET /api/v1/cost/summary.
// Deferred to cost-tracker integration (internal/cost/cost.go stub).
func (s *Server) handleGetCostSummary(w http.ResponseWriter, _ *http.Request) {
	writeError(w, "NOT_IMPLEMENTED", "not implemented in v0.1", http.StatusNotImplemented)
}
