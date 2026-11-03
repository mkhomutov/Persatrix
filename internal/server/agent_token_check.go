package server

// Agent tokens on REST calls (RFC 0009 Phases 3–4 plan PR 8, design points
// G-5 and G-7). Under auth.mode: enabled an X-Agent-Token proves which agent
// is calling, and the rate limiter, the circuit breaker and the publish
// handler's sender check go by that agent. Under disabled no token is
// checked: registration still issues them (agent_enrollment.go), but every
// caller is the anonymous `local` and agent IDs stay self-reported.

import (
	"errors"
	"net/http"
	"strconv"

	"github.com/mkhomutov/persatrix/internal/security"
)

// AgentTokenHeader is the header an agent shows its token in, on every REST
// call but its registration. It is not Authorization, which carries account
// sessions, so the two never share a header.
const AgentTokenHeader = "X-Agent-Token"

// The code of the 401 that refuses a call for its agent token, one for each
// way [security.AgentTokenIssuer.Check] refuses one. The agents tell a refused
// token from every other 401 by these codes alone, so their spelling is pinned
// to agents/orchestrator_session.py's by
// tests/unit/python/test_cross_language_agent_token_codes_drift.py. On an
// invalid or expired token an agent registers again; on a revoked one it
// stops, since an operator deregistered it. A token that a later registration
// of the same ID retired reads as revoked too (PR 7 review F-4): a code of its
// own would have an agent register again, and then a rolling restart's old
// process would take the ID back from the new one, each retiring the other's
// token until the registration limit stopped them both.
const AgentTokenInvalidCode = "AGENT_TOKEN_INVALID"

// AgentTokenExpiredCode: see [AgentTokenInvalidCode].
const AgentTokenExpiredCode = "AGENT_TOKEN_EXPIRED"

// AgentTokenRevokedCode: see [AgentTokenInvalidCode].
const AgentTokenRevokedCode = "AGENT_TOKEN_REVOKED"

// How many refused agent tokens one source may send in a window before it is
// told to wait. Each refusal writes an audit line that is flushed at once, so
// the limit bounds those flushes. A refused token proves no agent, so it is
// counted by the caller's address, and sized as registrations are
// (agent_enrollment.go), since an agent refused for its token registers next.
const (
	tokenRefusalsPerSourceCalls         = 120
	tokenRefusalsPerSourceWindowSeconds = 60
	tokenRefusalsPerSourceMaxTracked    = 1000
)

// errAgentTokenNamesAnother: a good token, shown beside an X-Agent-ID that
// names another agent. The agents read it as invalid.
var errAgentTokenNamesAnother = errors.New("agent token names another agent than X-Agent-ID")

// registerPath is the one route no token is checked on. Registration is how
// an agent gets a token, so a stale one it still shows must not stop the
// registration that replaces it.
const registerPath = "/api/v1/agents/register"

// checkAgentToken reads a request's X-Agent-Token under auth.mode: enabled
// and returns the agent it proves, or "" when the request shows none. It
// refuses the request, answering it and returning false, when the token is
// not good, when it names another agent than the request's X-Agent-ID, or
// when the request also shows an account credential: a request has one
// caller.
func (s *Server) checkAgentToken(w http.ResponseWriter, r *http.Request) (string, bool) {
	token := r.Header.Get(AgentTokenHeader)
	if token == "" || (r.Method == http.MethodPost && r.URL.Path == registerPath) {
		return "", true
	}
	named := r.Header.Get(security.AgentIDHeader)
	if _, _, account := presentedToken(r); account || r.Header.Get("Authorization") != "" {
		s.refuseAgentToken(w, r, named, "", "account_credential", http.StatusBadRequest,
			"BAD_REQUEST", "show an agent token or an account credential, not both")
		return "", false
	}
	claims, err := s.agentTokens.Check(token)
	if err == nil && named != "" && named != claims.AgentID {
		err = errAgentTokenNamesAnother
	}
	if err != nil {
		code, reason := agentTokenRefusal(err)
		s.refuseAgentToken(w, r, named, claims.AgentID, reason, http.StatusUnauthorized,
			code, "agent token refused")
		return "", false
	}
	return claims.AgentID, true
}

// agentTokenRefusal names a refused token's code, which the caller is told,
// and its reason, which the audit line records.
func agentTokenRefusal(err error) (code, reason string) {
	switch {
	case errors.Is(err, security.ErrAgentTokenExpired):
		return AgentTokenExpiredCode, "expired"
	case errors.Is(err, security.ErrAgentTokenRevoked):
		return AgentTokenRevokedCode, "revoked"
	case errors.Is(err, errAgentTokenNamesAnother):
		return AgentTokenInvalidCode, "agent_id_mismatch"
	}
	return AgentTokenInvalidCode, "invalid"
}

// refuseAgentToken answers a refused token with status and code, and writes
// an agent.token_invalid line naming the ID the request claimed and, when the
// token was good but named another agent, that agent; never the token. A
// source past its refusals for the window is answered 429 instead, with no
// line: the limiter's own rate_limit.violated line, at most one a window,
// stands for them.
func (s *Server) refuseAgentToken(w http.ResponseWriter, r *http.Request, named, tokenAgent, reason string,
	status int, code, msg string,
) {
	source := s.requestSource(r)
	if !s.tokenRefusalsPerSource.Allow(r.Context(), source) {
		w.Header().Set("Retry-After", strconv.Itoa(tokenRefusalsPerSourceWindowSeconds))
		writeError(w, "RATE_LIMITED", "too many refused agent tokens from this address", http.StatusTooManyRequests)
		return
	}
	ev := security.AuditEvent{
		EventType: security.AuditAgentTokenInvalid,
		Action:    "authenticate",
		Resource:  r.URL.Path,
		Outcome:   "refused",
		Detail:    map[string]any{"reason": reason, "method": r.Method, "source": source},
	}
	// Only an ID an agent could hold reaches the line: the header is the
	// caller's to fill, and the audit log is flushed line by line.
	if security.IssuableAgentID(named) {
		ev.AgentID = named
	}
	if tokenAgent != "" {
		ev.Detail["token_agent_id"] = tokenAgent
	}
	s.emitAudit(r.Context(), ev)
	writeError(w, code, msg, status)
}

// restAgentID is the agent a REST call's budget and quarantine go by
// ([security.WithAgentID]): under auth.mode: enabled the agent its token
// proves, and none for a call that shows no token, whatever its X-Agent-ID
// says; under disabled the X-Agent-ID it reports, as before.
func (s *Server) restAgentID(r *http.Request) string {
	if s.authEnforced() {
		return identityFrom(r.Context()).AgentID
	}
	return r.Header.Get(security.AgentIDHeader)
}

// initTokenRefusalLimiter builds the limiter on refused tokens per source.
// Called from New after the option loop, so the logger and auditor are
// settled.
func (s *Server) initTokenRefusalLimiter() error {
	var err error
	s.tokenRefusalsPerSource, err = security.NewRateLimiter(security.RateLimitConfig{
		CallsPerWindow:   tokenRefusalsPerSourceCalls,
		WindowSeconds:    tokenRefusalsPerSourceWindowSeconds,
		MaxTrackedAgents: tokenRefusalsPerSourceMaxTracked,
		Enabled:          true,
		Logger:           s.logger,
		Auditor:          s.auditor,
	})
	return err
}
