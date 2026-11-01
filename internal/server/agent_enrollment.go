package server

// Agent enrollment (RFC 0009 Phases 3–4 plan PR 6): registration checks the
// fleet enrollment secret and answers with an agent token, and
// deregistration revokes the agent's tokens. The route handlers in
// agent_handlers.go call these; the registry and token steps themselves are
// [security.AgentEnrollment]'s.

import (
	"context"
	"net"
	"net/http"
	"strconv"
	"time"

	"github.com/mkhomutov/persatrix/internal/registry"
	"github.com/mkhomutov/persatrix/internal/security"
)

// EnrollmentSecretHeader is the header an agent shows the fleet enrollment
// secret in when it registers. It is not Authorization, which carries
// account sessions, so the two never share a header.
const EnrollmentSecretHeader = "X-Enrollment-Secret"

// How many registrations one source may make in a window (PR 6 review).
// Registration is open to anyone, and each one writes an audit line that is
// flushed at once, so the limit bounds both secret guessing and those
// flushes. It is counted by the caller's address, not the X-Agent-ID it
// picks, and sized so a host's whole fleet can register at once.
const (
	registerPerSourceCalls         = 120
	registerPerSourceWindowSeconds = 60
	registerPerSourceMaxTracked    = 1000
)

// registerAgentResponse is what registration answers with: the agent as the
// other agent routes show it, plus the token the agent shows on its later
// calls and when that token expires. Only registration carries a token.
type registerAgentResponse struct {
	agentResponse
	AgentToken          string    `json:"agent_token"`
	AgentTokenExpiresAt time.Time `json:"agent_token_expires_at"`
}

// initAgentEnrollment settles the server's enrollment and its token issuer,
// and builds the registration limiter. Called from New after the option
// loop, so the registry, logger and auditor are settled. A server given an
// enrollment signs with its issuer; one given none makes its own over its
// registry.
func (s *Server) initAgentEnrollment() error {
	if s.enrollment != nil {
		s.agentTokens = s.enrollment.Issuer()
	} else {
		if s.agentTokens == nil {
			s.agentTokens = security.NewAgentTokenIssuer()
		}
		s.enrollment = security.NewAgentEnrollment(s.registry, s.agentTokens)
	}
	var err error
	s.registerPerSource, err = security.NewRateLimiter(security.RateLimitConfig{
		CallsPerWindow:   registerPerSourceCalls,
		WindowSeconds:    registerPerSourceWindowSeconds,
		MaxTrackedAgents: registerPerSourceMaxTracked,
		Enabled:          true,
		Logger:           s.logger,
		Auditor:          s.auditor,
	})
	return err
}

// registrationThrottled reports whether source has used its registrations
// for the window, and if so answers 429. It writes no audit line: the
// limiter's own rate_limit.violated line, at most one a window, stands for
// them.
func (s *Server) registrationThrottled(w http.ResponseWriter, r *http.Request, source string) bool {
	if s.registerPerSource.Allow(r.Context(), source) {
		return false
	}
	w.Header().Set("Retry-After", strconv.Itoa(registerPerSourceWindowSeconds))
	writeError(w, "RATE_LIMITED", "too many registrations from this address", http.StatusTooManyRequests)
	return true
}

// enrollmentRefused reports whether the request shows a fleet enrollment
// secret other than the one set, and if so answers 401 and writes an
// agent.token_invalid audit line, which names neither secret. A request that
// shows no secret is admitted, so the fleet keeps registering until its
// agents send one (plan PR 7) and PR 9 requires it under auth.mode: enabled;
// its agent.token_issued line says none was shown. With no secret set there
// is nothing to compare, so a shown one is ignored.
func (s *Server) enrollmentRefused(w http.ResponseWriter, r *http.Request, agentID, source string) bool {
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
			"source": source,
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

// auditTokenIssued writes the agent.token_issued line for a registration
// from source, never the token itself. A registration of an agent already
// registered says its earlier token stopped working, and, when it gave
// another address or came from another source, which: whoever holds the
// fleet secret can register under any ID and copy any address, so an
// operator can see a live agent's ID taken from elsewhere (G-14). With a
// secret set, the line says whether this registration showed it.
func (s *Server) auditTokenIssued(r *http.Request, info registry.AgentInfo, source string,
	enrolled security.AgentEnrolled,
) {
	detail := map[string]any{
		"address":    info.Address,
		"source":     source,
		"expires_at": enrolled.Claims.ExpiresAt.Format(time.RFC3339),
	}
	if previous := enrolled.Previous; previous != nil {
		detail["previous_token"] = "revoked"
		if previous.Address != info.Address {
			detail["previous_address"] = previous.Address
		}
		if enrolled.PreviousSource != "" && enrolled.PreviousSource != source {
			detail["previous_source"] = enrolled.PreviousSource
		}
	}
	if s.enrollmentSecret != "" {
		detail["enrollment_secret"] = "absent"
		if r.Header.Get(EnrollmentSecretHeader) != "" {
			detail["enrollment_secret"] = "shown"
		}
	}
	s.emitAudit(r.Context(), security.AuditEvent{
		EventType: security.AuditAgentTokenIssued,
		AgentID:   info.ID,
		Action:    "register",
		Resource:  info.ID,
		Outcome:   "issued",
		Detail:    detail,
	})
}

// auditDeregistered writes the agent.deregistered line for an agent removed
// and its tokens revoked. Anyone can deregister an agent, so the line names
// where the request came from.
func (s *Server) auditDeregistered(ctx context.Context, id, source string) {
	s.emitAudit(ctx, security.AuditEvent{
		EventType: security.AuditAgentDeregistered,
		AgentID:   id,
		Action:    "deregister",
		Resource:  id,
		Outcome:   "revoked",
		Detail:    map[string]any{"source": source},
	})
}
