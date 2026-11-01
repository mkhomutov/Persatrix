package server

// Agent enrollment (RFC 0009 Phases 3–4 plan PR 6): registration checks the
// fleet enrollment secret and answers with an agent token, and
// deregistration revokes the agent's tokens. The route handlers in
// agent_handlers.go call these.

import (
	"context"
	"errors"
	"net"
	"net/http"
	"time"

	"github.com/mkhomutov/persatrix/internal/registry"
	"github.com/mkhomutov/persatrix/internal/security"
)

// EnrollmentSecretHeader is the header an agent shows the fleet enrollment
// secret in when it registers. It is not Authorization, which carries
// account sessions, so the two never share a header.
const EnrollmentSecretHeader = "X-Enrollment-Secret"

// registerAgentResponse is what registration answers with: the agent as the
// other agent routes show it, plus the token the agent shows on its later
// calls and when that token expires. Only registration carries a token.
type registerAgentResponse struct {
	agentResponse
	AgentToken          string    `json:"agent_token"`
	AgentTokenExpiresAt time.Time `json:"agent_token_expires_at"`
}

// enrollmentRefused reports whether the request shows a fleet enrollment
// secret other than the one set, and if so answers 401 and writes an
// agent.token_invalid audit line, which names neither secret. A request that
// shows no secret is admitted, so the fleet keeps registering until its
// agents send one (plan PR 7) and PR 9 requires it under auth.mode: enabled.
// With no secret set there is nothing to compare, so a shown one is ignored.
func (s *Server) enrollmentRefused(w http.ResponseWriter, r *http.Request, agentID string) bool {
	shown := r.Header.Get(EnrollmentSecretHeader)
	if s.enrollmentSecret == "" || shown == "" || secretsEqual(shown, s.enrollmentSecret) {
		return false
	}
	s.emitAudit(r.Context(), security.AuditEvent{
		EventType: security.AuditAgentTokenInvalid,
		AgentID:   agentID,
		Action:    "register",
		Resource:  agentID,
		Outcome:   "refused",
		Detail: map[string]any{
			"reason": "enrollment_secret",
			"source": s.requestSource(r),
		},
	})
	writeError(w, "UNAUTHORIZED", "invalid enrollment secret", http.StatusUnauthorized)
	return true
}

// requestSource is the address a request came from, read past the trusted
// proxies auth is configured with, as the login throttle reads it.
func (s *Server) requestSource(r *http.Request) string {
	var trusted []*net.IPNet
	if s.auth != nil {
		trusted = s.auth.cfg.TrustedProxies
	}
	return clientIP(r, trusted)
}

// enrollAgent registers info and issues its agent a token, with no
// deregistration of any agent able to fall between the two (PR 5 review
// F-6). previous is the agent as it was registered before, or nil. The
// token retires the agent's earlier ones (PR 5 review F-12).
func (s *Server) enrollAgent(ctx context.Context, info registry.AgentInfo) (
	token string, claims security.AgentTokenClaims, previous *registry.AgentInfo, err error,
) {
	s.enrollMu.Lock()
	defer s.enrollMu.Unlock()
	previous, err = s.registry.Get(ctx, info.ID)
	if errors.Is(err, registry.ErrAgentNotFound) {
		previous, err = nil, nil
	}
	if err != nil {
		return "", security.AgentTokenClaims{}, nil, err
	}
	if err = s.registry.Register(ctx, info); err != nil {
		return "", security.AgentTokenClaims{}, nil, err
	}
	token, claims, err = s.agentTokens.Issue(info.ID)
	return token, claims, previous, err
}

// deregisterAgent removes the agent and revokes its tokens, with no
// registration able to fall between the two.
func (s *Server) deregisterAgent(ctx context.Context, id string) error {
	s.enrollMu.Lock()
	defer s.enrollMu.Unlock()
	if err := s.registry.Unregister(ctx, id); err != nil {
		return err
	}
	s.agentTokens.Revoke(id)
	return nil
}

// auditTokenIssued writes the agent.token_issued line for a registration,
// never the token itself. A registration of an agent already registered
// says its earlier token stopped working, and, when it came from another
// address, which one: whoever holds the fleet secret can register under any
// ID, so an operator can see a live agent's ID taken from elsewhere (G-14).
func (s *Server) auditTokenIssued(ctx context.Context, info registry.AgentInfo,
	claims security.AgentTokenClaims, previous *registry.AgentInfo,
) {
	detail := map[string]any{
		"address":    info.Address,
		"expires_at": claims.ExpiresAt.Format(time.RFC3339),
	}
	if previous != nil {
		detail["previous_token"] = "revoked"
		if previous.Address != info.Address {
			detail["previous_address"] = previous.Address
		}
	}
	s.emitAudit(ctx, security.AuditEvent{
		EventType: security.AuditAgentTokenIssued,
		AgentID:   info.ID,
		Action:    "register",
		Resource:  info.ID,
		Outcome:   "issued",
		Detail:    detail,
	})
}
