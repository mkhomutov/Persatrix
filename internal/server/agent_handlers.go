package server

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"regexp"

	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/registry"
	"github.com/mkhomutov/persatrix/internal/security"
)

// capabilityNameRegex bounds the per-capability identifier surface so a
// malformed or hostile registration cannot push arbitrary strings (e.g.
// secret-shaped tokens, control characters, or 1-MiB ANSI sequences) into
// the registry, audit log, and downstream prompts. The pattern matches
// the [docs/ai-glossary.md] capability-name convention: lowercase
// alphanumeric with `_` or `-` separators, 1–64 chars.
var capabilityNameRegex = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,63}$`)

// maxCapabilitiesPerAgent caps the per-registration capability slice to
// bound the audit-side work performed by validateCapabilities.
//
// PR #234 review M-4: each malformed capability triggers a security-class
// audit emit (synchronous fsync under the audit logger's mutex). Without
// a cap, a single hostile registration with N bogus entries fan-outs to
// N serialised fsyncs, blocking every other audit emit site in the
// orchestrator until the registration handler returns. 64 is the same
// magnitude as the per-name length bound and is generous for realistic
// agents (the largest blueprint capability lists in templates/ are well
// under a dozen).
const maxCapabilitiesPerAgent = 64

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
	// Agent IDs share the same format as workflow IDs (^[a-z0-9]([a-z0-9-]*[a-z0-9])?$).
	// Uses resourceIDRegex directly — single source of truth from planner package.
	if !resourceIDRegex.MatchString(req.ID) {
		writeError(w, "BAD_REQUEST", "id must match ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$", http.StatusBadRequest)
		return
	}
	// (PR #71 review §2.3): cap display name length to prevent registry pollution
	// and log bloat. 100 chars is generous for a human-readable label. The name
	// is display-only — no routing or security decisions depend on it.
	if len(req.Name) > 100 {
		writeError(w, "BAD_REQUEST", "name exceeds maximum length of 100 characters", http.StatusBadRequest)
		return
	}
	// TODO(v0.2): validate address format (host:port or URI scheme).
	// Currently any non-empty string up to 253 characters is accepted per RFC 0002 v0.1 scope.
	if req.Address == "" {
		writeError(w, "BAD_REQUEST", "address is required", http.StatusBadRequest)
		return
	}
	// (PR #16 carry-forward F-01): Enforce max length to prevent registry pollution
	// and log bloat. 253 aligns with DNS hostname max length (RFC 1035).
	// NOTE(review-F05): len() measures bytes, not runes. This is intentional —
	// v0.1 addresses are ASCII host:port strings where bytes == characters, and
	// byte-based enforcement provides defense-in-depth against oversized payloads
	// regardless of encoding.
	if len(req.Address) > 253 {
		writeError(w, "BAD_REQUEST", "address exceeds maximum length of 253 characters", http.StatusBadRequest)
		return
	}

	// RFC 0009 PR 1b — reject ill-formed capability names at the boundary so
	// unbounded / hostile strings cannot enter the registry, audit log, or
	// prompt context. Each rejection emits `capability.violation` (security-
	// class, fsync’d immediately) so an operator can correlate noisy clients.
	//
	// PR #234 review M-4: bound the slice length first. Without this an
	// attacker can submit thousands of bogus capabilities and amplify a
	// single registration into thousands of synchronous fsyncs serialised
	// under the audit logger's mutex (DoS amplification that bypasses the
	// per-request HTTP timeout because the work happens inside the audit
	// logger, not the handler). When the cap is exceeded we emit one
	// `capability.violation` with reason="too_many" carrying the offending
	// count, then short-circuit — operators still get forensic visibility
	// without paying the per-entry fsync cost.
	if len(req.Capabilities) > maxCapabilitiesPerAgent {
		s.emitAudit(r.Context(), security.AuditEvent{
			EventType: security.AuditCapabilityViolation,
			AgentID:   req.ID,
			Action:    "register",
			Resource:  "capability",
			Detail: map[string]any{
				"reason": "too_many",
				"count":  len(req.Capabilities),
				"limit":  maxCapabilitiesPerAgent,
			},
		})
		writeError(w, "BAD_REQUEST", fmt.Sprintf("capabilities exceeds maximum of %d entries", maxCapabilitiesPerAgent), http.StatusBadRequest)
		return
	}
	if violations := s.validateCapabilities(r.Context(), req.ID, req.Capabilities); len(violations) > 0 {
		writeError(w, "BAD_REQUEST", "capability "+violations[0]+" must match ^[a-z0-9][a-z0-9_-]{0,63}$", http.StatusBadRequest)
		return
	}

	info := registry.AgentInfo{
		ID:           req.ID,
		Name:         req.Name,
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

	s.emitAudit(r.Context(), security.AuditEvent{
		EventType: security.AuditAgentRegistered,
		AgentID:   req.ID,
		Action:    "register",
		Resource:  req.Address,
		Detail: map[string]any{
			"capabilities": req.Capabilities,
			"name":         req.Name,
		},
	})

	writeJSON(w, agentToResponse(&info), http.StatusCreated)
}

// validateCapabilities checks each capability name against the documented
// charset/length contract and emits `capability.violation` for every
// rejected entry. Returns the list of rejected names in input order so the
// caller can surface a deterministic error message.
func (s *Server) validateCapabilities(ctx context.Context, agentID string, caps []string) []string {
	var bad []string
	for _, c := range caps {
		if capabilityNameRegex.MatchString(c) {
			continue
		}
		bad = append(bad, c)
		s.emitAudit(ctx, security.AuditEvent{
			EventType: security.AuditCapabilityViolation,
			AgentID:   agentID,
			Action:    "register",
			Resource:  "capability",
			Detail: map[string]any{
				"capability": c,
				"reason":     "format",
			},
		})
	}
	return bad
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
	if !resourceIDRegex.MatchString(id) {
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
	if !resourceIDRegex.MatchString(id) {
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
		Name:    a.Name,
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
