package server

import "net/http"

// handleGetLogs handles GET /api/v1/executions/{id}/logs.
// Deferred to RFC 0003 — the Executor will capture step-level logs.
// TODO(v0.3): validate execution ID format before querying
func (s *Server) handleGetLogs(w http.ResponseWriter, _ *http.Request) {
	writeError(w, "NOT_IMPLEMENTED", "not implemented in v0.1", http.StatusNotImplemented)
}
