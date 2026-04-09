package server

import (
	"errors"
	"net/http"

	"go.uber.org/zap"

	"github.com/orchestr8/orchestr8/internal/registry"
)

// handleRegisterAgent handles POST /api/v1/agents/register.
func (s *Server) handleRegisterAgent(w http.ResponseWriter, r *http.Request) {
	if !requireJSON(w, r) {
		return
	}
	var req registerAgentRequest
	if !decodeJSON(w, r, &req) {
		return
	}

	if req.ID == "" {
		writeError(w, "BAD_REQUEST", "id is required", http.StatusBadRequest)
		return
	}
	// Agent IDs share the same format as workflow IDs (^[a-z0-9][a-z0-9-]*[a-z0-9]$).
	// Uses workflowIDRegex directly — single source of truth from planner package.
	if !workflowIDRegex.MatchString(req.ID) {
		writeError(w, "BAD_REQUEST", "id must match ^[a-z0-9][a-z0-9-]*[a-z0-9]$", http.StatusBadRequest)
		return
	}
	// TODO(v0.2): validate address format (host:port or URI scheme) and enforce
	// max length. Currently any non-empty string is accepted per RFC 0002 v0.1 scope.
	if req.Address == "" {
		writeError(w, "BAD_REQUEST", "address is required", http.StatusBadRequest)
		return
	}

	info := registry.AgentInfo{
		ID:           req.ID,
		Address:      req.Address,
		Capabilities: req.Capabilities,
		Status:       registry.StatusHealthy, // reachable until first health check fails
	}

	if err := s.registry.Register(r.Context(), info); err != nil {
		if errors.Is(err, registry.ErrAgentAlreadyRegistered) {
			writeError(w, "CONFLICT", "agent already registered", http.StatusConflict)
			return
		}
		s.logger.Error("failed to register agent", zap.Error(err), zap.String("agent_id", req.ID))
		writeError(w, "INTERNAL", "failed to register agent", http.StatusInternalServerError)
		return
	}

	writeJSON(w, agentToResponse(&info), http.StatusCreated)
}

// handleListAgents handles GET /api/v1/agents.
func (s *Server) handleListAgents(w http.ResponseWriter, r *http.Request) {
	agents, err := s.registry.List(r.Context())
	if err != nil {
		s.logger.Error("failed to list agents", zap.Error(err))
		writeError(w, "INTERNAL", "failed to list agents", http.StatusInternalServerError)
		return
	}

	resp := make([]agentResponse, 0, len(agents))
	for i := range agents {
		resp = append(resp, agentToResponse(&agents[i]))
	}
	writeJSON(w, resp, http.StatusOK)
}

// handleGetAgent handles GET /api/v1/agents/{id}.
func (s *Server) handleGetAgent(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	// Validate agent ID format at the handler boundary — defense-in-depth consistent
	// with validateRunID() on workflow endpoints. Prevents arbitrary strings from
	// reaching the registry layer, important for v0.2 SQLite migration.
	// (Review finding F-01)
	if !workflowIDRegex.MatchString(id) {
		writeError(w, "BAD_REQUEST", "invalid agent ID format", http.StatusBadRequest)
		return
	}

	agent, err := s.registry.Get(r.Context(), id)
	if err != nil {
		if errors.Is(err, registry.ErrAgentNotFound) {
			writeError(w, "NOT_FOUND", "agent not found", http.StatusNotFound)
			return
		}
		s.logger.Error("failed to get agent", zap.Error(err), zap.String("agent_id", id))
		writeError(w, "INTERNAL", "failed to get agent", http.StatusInternalServerError)
		return
	}

	writeJSON(w, agentToResponse(agent), http.StatusOK)
}

// handleDeleteAgent handles DELETE /api/v1/agents/{id}.
func (s *Server) handleDeleteAgent(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	// Validate agent ID format — consistent with handleGetAgent. (Review finding F-01)
	if !workflowIDRegex.MatchString(id) {
		writeError(w, "BAD_REQUEST", "invalid agent ID format", http.StatusBadRequest)
		return
	}

	// TODO(v0.3): consider checking for active workflow runs referencing this agent
	// before deletion, analogous to the "cannot delete running workflow" guard in
	// handleDeleteWorkflow. Currently dormant — no execution in v0.1. (Review finding F-09)
	if err := s.registry.Unregister(r.Context(), id); err != nil {
		if errors.Is(err, registry.ErrAgentNotFound) {
			writeError(w, "NOT_FOUND", "agent not found", http.StatusNotFound)
			return
		}
		s.logger.Error("failed to delete agent", zap.Error(err), zap.String("agent_id", id))
		writeError(w, "INTERNAL", "failed to delete agent", http.StatusInternalServerError)
		return
	}

	w.WriteHeader(http.StatusNoContent)
}

// agentToResponse converts a registry.AgentInfo to a wire-format agentResponse.
func agentToResponse(a *registry.AgentInfo) agentResponse {
	resp := agentResponse{
		ID:      a.ID,
		Address: a.Address,
		Status:  agentStatusString(a.Status),
	}
	// Ensure capabilities serializes as [] not null.
	if a.Capabilities != nil {
		resp.Capabilities = a.Capabilities
	} else {
		resp.Capabilities = []string{}
	}
	return resp
}

// agentStatusString maps AgentStatus to lowercase JSON wire-format strings.
func agentStatusString(s registry.AgentStatus) string {
	switch s {
	case registry.StatusHealthy:
		return "healthy"
	case registry.StatusDegraded:
		return "degraded"
	case registry.StatusOffline:
		return "offline"
	default:
		return "unknown"
	}
}
