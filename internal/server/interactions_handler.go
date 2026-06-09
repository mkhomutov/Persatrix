package server

import (
	"errors"
	"net/http"

	"go.uber.org/zap"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/mkhomutov/persatrix/internal/executor"
	"github.com/mkhomutov/persatrix/internal/generated/taskpb"
	"github.com/mkhomutov/persatrix/internal/registry"
)

// defaultClosedInteractionsLimit is the page size when ?limit is omitted.
// The agent-side query clamps to its own MAX_RECALL_LIMIT regardless.
const defaultClosedInteractionsLimit = 20

// WithInteractionReader injects an InteractionReader for the
// closed-interaction summary endpoint (v0.3.8 interaction-summary surface).
func WithInteractionReader(ir executor.InteractionReader) ServerOption {
	return func(s *Server) {
		s.interactionReader = ir
	}
}

type closedInteractionDTO struct {
	InteractionID string  `json:"interaction_id"`
	Scope         string  `json:"scope"`
	StartedAt     float64 `json:"started_at"`
	ClosedAt      float64 `json:"closed_at"`
	TurnCount     int32   `json:"turn_count"`
	CloseReason   string  `json:"close_reason"`
	Summary       string  `json:"summary"`
}

type closedInteractionsResponse struct {
	Interactions []closedInteractionDTO `json:"interactions"`
}

// handleGetClosedInteractions handles
// GET /api/v1/agents/{id}/interactions/closed?scope=&interaction_id=&limit=
//
// v0.3.8 interaction-summary surface (RFC 0020 §C/§D). Returns the agent's
// closed-interaction summaries newest-first so the web console + CLI can
// render the synthesised outcome of a converged brainstorm. Read-only: the
// summary is generated agent-side at interaction close; this proxies the
// agent's GetClosedInteractions gRPC and projects it to JSON. The
// "[interaction summary unavailable]" sentinel is forwarded verbatim so a
// failed summary is shown honestly rather than blanked.
func (s *Server) handleGetClosedInteractions(w http.ResponseWriter, r *http.Request) {
	if s.interactionReader == nil {
		writeError(w, "UNAVAILABLE", "interaction reader not configured", http.StatusServiceUnavailable)
		return
	}

	agentID := r.PathValue("id")
	if agentID == "" {
		writeError(w, "BAD_REQUEST", "agent_id is required", http.StatusBadRequest)
		return
	}
	if !resourceIDRegex.MatchString(agentID) {
		writeError(w, "BAD_REQUEST", "invalid agent ID format", http.StatusBadRequest)
		return
	}

	limit, err := parseLimit(r, defaultClosedInteractionsLimit)
	if err != nil {
		writeError(w, "BAD_REQUEST", err.Error(), http.StatusBadRequest)
		return
	}

	req := &taskpb.ClosedInteractionsRequest{
		AgentId:       agentID,
		Scope:         r.URL.Query().Get("scope"),
		InteractionId: r.URL.Query().Get("interaction_id"),
		Limit:         int32(limit),
	}
	resp, err := s.interactionReader.GetClosedInteractions(r.Context(), agentID, req)
	if err != nil {
		switch {
		case errors.Is(err, registry.ErrAgentNotFound):
			writeError(w, "NOT_FOUND", "agent not found", http.StatusNotFound)
		case errors.Is(err, executor.ErrAgentNotReady):
			writeError(w, "UNAVAILABLE", "agent not ready", http.StatusServiceUnavailable)
		default:
			// The reader makes a live gRPC call, so most errors arrive as
			// gRPC *status* errors rather than the executor's Go sentinels:
			// the agent-side servicer returns NOT_FOUND for an id unknown to
			// the agent process / INVALID_ARGUMENT for a malformed request,
			// and the transport returns Unavailable / DeadlineExceeded when
			// the agent is down or slow. Map those to the matching HTTP
			// status instead of a blanket 500 (which would mislabel an
			// agent-down or not-found condition as an internal fault).
			if st, ok := status.FromError(err); ok {
				switch st.Code() {
				case codes.NotFound:
					writeError(w, "NOT_FOUND", "agent not found", http.StatusNotFound)
					return
				case codes.InvalidArgument:
					writeError(w, "BAD_REQUEST", "invalid request", http.StatusBadRequest)
					return
				case codes.Unavailable, codes.DeadlineExceeded:
					writeError(w, "UNAVAILABLE", "agent unavailable", http.StatusServiceUnavailable)
					return
				}
			}
			s.logger.Error("interactions: closed read failed",
				zap.String("agent_id", agentID), zap.Error(err))
			writeError(w, "INTERNAL", "failed to read closed interactions", http.StatusInternalServerError)
		}
		return
	}

	out := closedInteractionsResponse{
		Interactions: make([]closedInteractionDTO, 0, len(resp.GetInteractions())),
	}
	for _, it := range resp.GetInteractions() {
		out.Interactions = append(out.Interactions, closedInteractionDTO{
			InteractionID: it.GetInteractionId(),
			Scope:         it.GetScope(),
			StartedAt:     it.GetStartedAt(),
			ClosedAt:      it.GetClosedAt(),
			TurnCount:     it.GetTurnCount(),
			CloseReason:   it.GetCloseReason(),
			Summary:       it.GetSummary(),
		})
	}
	writeJSON(w, out, http.StatusOK)
}
