package server

import (
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"errors"
	"net/http"
	"regexp"
	"strconv"
	"strings"

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
	// RFC 0048 amendment §A — role is a display-only persona cue (like name);
	// cap it on the same rationale to prevent registry pollution and log bloat.
	if len(req.Role) > 100 {
		writeError(w, "BAD_REQUEST", "role exceeds maximum length of 100 characters", http.StatusBadRequest)
		return
	}
	// RFC 0048 amendment §A DTO — type is a short agent-kind token ("task"/"persona"/…)
	// used as display/affordance metadata only. Cap it tightly (a token, not prose)
	// on the same registry-pollution rationale. An unknown or empty value is not
	// rejected: the console treats anything other than "task" as chattable, so a
	// new kind degrades safely without a server change.
	if len(req.Type) > 32 {
		writeError(w, "BAD_REQUEST", "type exceeds maximum length of 32 characters", http.StatusBadRequest)
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
		// PR #234 review N-1: adjacent error messages on this handler use
		// plain string concat; align style and drop the unused fmt import.
		writeError(w, "BAD_REQUEST", "capabilities exceeds maximum of "+strconv.Itoa(maxCapabilitiesPerAgent)+" entries", http.StatusBadRequest)
		return
	}
	if violations := s.validateCapabilities(r.Context(), req.ID, req.Capabilities); len(violations) > 0 {
		writeError(w, "BAD_REQUEST", "capability "+violations[0]+" must match ^[a-z0-9][a-z0-9_-]{0,63}$", http.StatusBadRequest)
		return
	}

	info := registry.AgentInfo{
		ID:           req.ID,
		Name:         req.Name,
		Role:         req.Role, // RFC 0048 amendment §A — optional persona role, "" when unset
		Type:         req.Type, // RFC 0048 amendment §A DTO — optional agent kind, "" when unset
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

	// PR #234 review N-1: use the agent ID (not the address) as Resource
	// for forensic stability. Addresses rotate on redeploy / port change,
	// so the agent ID is the durable anchor that joins this event to
	// downstream `tool.invoked` / `capability.violation` records (which
	// already use agent_id as Resource per dispatch.go). The address is
	// still useful for incident response, so it moves to Detail["address"].
	s.emitAudit(r.Context(), security.AuditEvent{
		EventType: security.AuditAgentRegistered,
		AgentID:   req.ID,
		Action:    "register",
		Resource:  req.ID,
		Detail: map[string]any{
			"address":      req.Address,
			"capabilities": req.Capabilities,
			"name":         req.Name,
		},
	})

	writeJSON(w, agentToResponse(&info), http.StatusCreated)
}

// maxCapabilityEchoLen bounds the verbatim echo of a rejected capability
// name back into the audit event Detail.
//
// PR #234 review N-2: the secret redactor scrubs known patterns, but a
// hostile registration carrying a capability value that is e.g. 1 MiB of
// arbitrary bytes would otherwise be written verbatim to the audit log
// (after redaction, but redaction does not truncate). Combined with the
// per-slice cap (maxCapabilitiesPerAgent) this keeps the worst-case
// audit-write size from a single registration bounded at
// 64 * 256 = 16 KiB rather than 64 * client_max_body_size. 256 chars is
// generous compared to the 64-char regex-enforced legal upper bound, so
// rejected values that are merely "off by a few characters" survive
// intact for operator triage.
const maxCapabilityEchoLen = 256

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
		echoed := c
		truncated := false
		if len(echoed) > maxCapabilityEchoLen {
			echoed = echoed[:maxCapabilityEchoLen]
			truncated = true
		}
		detail := map[string]any{
			"capability": echoed,
			"reason":     "format",
		}
		if truncated {
			detail["truncated"] = true
			detail["original_length"] = len(c)
		}
		s.emitAudit(ctx, security.AuditEvent{
			EventType: security.AuditCapabilityViolation,
			AgentID:   agentID,
			Action:    "register",
			Resource:  "capability",
			Detail:    detail,
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

// handleUnquarantineAgent handles POST /api/v1/agents/{id}/unquarantine
// (RFC 0009 PR 2). The endpoint is operator-facing: it releases an
// agent that has been quarantined by the [security.CircuitBreaker]
// after sustained policy violations. Returns 404 when the agent is
// not currently quarantined and 503 when no breaker is wired.
//
// `actor` is read from the standard X-Agent-ID header so the audit
// event records who initiated the release; defaults to "operator"
// when the header is absent.
//
// SECURITY (PR #244 review H-01): this endpoint has no authentication
// or authorization in v0.3.0 because the entire REST surface is
// unauthenticated until token validation lands in RFC 0009 Phase 4.
// The endpoint is uniquely sensitive — it undoes a security control —
// so deployments MUST front the orchestrator with an authenticating
// reverse proxy (or restrict the route at the network layer) until
// Phase 4 ships. Tracked alongside the broader X-Agent-ID spoofing
// gap documented in RFC 0009 §B.
//
// PR #244 review H-02 — defense-in-depth stop-gap: when the operator
// configures `SECURITY_UNQUARANTINE_TOKEN` (wired via
// [WithUnquarantineToken]), this handler additionally requires
// `Authorization: Bearer <token>` and rejects with 401 on missing or
// mismatched tokens. The comparison runs in constant time
// (crypto/subtle.ConstantTimeCompare) to avoid leaking the token
// byte-by-byte through response timing. The check is purely additive:
// when the token is unset, behaviour is unchanged.
func (s *Server) handleUnquarantineAgent(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if !resourceIDRegex.MatchString(id) {
		writeError(w, "BAD_REQUEST", "invalid agent ID format", http.StatusBadRequest)
		return
	}
	if s.circuitBreaker == nil {
		writeError(w, "UNAVAILABLE", "circuit breaker not configured", http.StatusServiceUnavailable)
		return
	}
	// PR #244 H-02: optional shared-secret gate. Only enforced when the
	// operator has opted in via SECURITY_UNQUARANTINE_TOKEN; an empty
	// configured token leaves the endpoint unauthenticated (matches the
	// pre-PR baseline + the documented reverse-proxy posture).
	if s.unquarantineToken != "" {
		if !validBearerToken(r.Header.Get("Authorization"), s.unquarantineToken) {
			writeError(w, "UNAUTHORIZED", "invalid or missing unquarantine token", http.StatusUnauthorized)
			return
		}
	}
	actor := r.Header.Get(security.AgentIDHeader)
	if actor == "" {
		actor = "operator"
	}
	if !s.circuitBreaker.Unquarantine(r.Context(), id, actor) {
		writeError(w, "NOT_FOUND", "agent is not quarantined", http.StatusNotFound)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

// validBearerToken returns true when the Authorization header carries a
// `Bearer <token>` whose token component is byte-for-byte equal to
// expected. The comparison is constant-time so a timing side channel
// cannot leak either the content OR the length of the expected token.
// expected must be non-empty (callers short-circuit when no token is
// configured); a zero-length expected would otherwise compare equal to
// any zero-length supplied token.
//
// ISSUE-0004: both inputs are hashed to fixed-size SHA-256 digests
// before subtle.ConstantTimeCompare. The previous implementation
// short-circuited on len(supplied) != len(expected), making the
// "wrong length" path observably faster than "wrong content" and
// letting a remote attacker probe the expected token length via
// differential response timing. Hashing first makes the comparison
// inputs identically sized regardless of the supplied token's length,
// so length and content cases are now indistinguishable.
func validBearerToken(header, expected string) bool {
	const prefix = "Bearer "
	if expected == "" {
		return false
	}
	if !strings.HasPrefix(header, prefix) {
		return false
	}
	supplied := header[len(prefix):]
	supSum := sha256.Sum256([]byte(supplied))
	expSum := sha256.Sum256([]byte(expected))
	return subtle.ConstantTimeCompare(supSum[:], expSum[:]) == 1
}

func agentToResponse(a *registry.AgentInfo) agentResponse {
	resp := agentResponse{
		ID:      a.ID,
		Name:    a.Name,
		Role:    a.Role, // RFC 0048 amendment §A — persona role; "" when unset
		Type:    a.Type, // RFC 0048 amendment §A DTO — agent kind; "" when unset
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
