package server

import (
	"net/http"
)

// handleGetCostSummaryImpl handles GET /api/v1/cost/summary.
// Returns a global cost summary including daily totals and top agents by spend.
// Requires a CostReporter to be wired — returns 503 if cost tracking is not configured.
func (s *Server) handleGetCostSummaryImpl(w http.ResponseWriter, _ *http.Request) {
	if s.costReporter == nil {
		writeError(w, "SERVICE_UNAVAILABLE", "cost tracking is not configured", http.StatusServiceUnavailable)
		return
	}

	summary := s.costReporter.GlobalSummary()
	writeJSON(w, summary, http.StatusOK)
}
