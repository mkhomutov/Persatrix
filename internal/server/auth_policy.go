package server

import "net/http"

// The §E per-route policy table (RFC 0039 Phase 2) — the route-by-route
// assignment that folds the design review's OQ #6. Three groups:
//
//   - public — identity-free surfaces (health, login, the console
//     shell), AND the agent-attributable REST ingress the §Non-Goals
//     place on the RFC 0009 track: agent self-registration and
//     self-deregistration (agents/server.py), and the RFC 0011 channel
//     HTTP seams the persona fleet drives in production
//     (channel_publisher / channel_history_fetcher / channel_catchup /
//     convene_client). Agents hold no accounts, so gating these would
//     break every deployed persona the moment auth.mode flips to
//     enabled; they stay open, defended by the RFC 0009 per-agent
//     limiter + quarantine, and their authorization story arrives with
//     RFC 0009 agent tokens. The residual is deliberate, WARN'd at
//     startup (cmd/orchestrator warnAuthPosture), and recorded in the
//     RFC's §Non-Goals — not an oversight.
//
//   - authenticated — the human read surface plus chat (§K: chatting is
//     what the `user` role exists for).
//
//   - operator — everything that mutates shared state (workflow runs,
//     deletions, channel/member/config administration, sessions), plus
//     unquarantine (§H) and persona-memory recall (read-shaped but a
//     memory-exposure surface; fail closed at the coarse gate until a
//     finer-grained story exists).
//
// Anything unregistered resolves to operator — the §E fail-closed
// default, so a newly added handler is never accidentally world-open.

// policyHandler is a sentinel http.Handler carrying a route's policy.
// It is registered on the policy mux purely to be *found*; it is never
// invoked.
type policyHandler struct{ policy routePolicy }

func (policyHandler) ServeHTTP(http.ResponseWriter, *http.Request) {}

// policyMux resolves a request to its §E policy using ServeMux's own
// pattern semantics — method-aware, {id} wildcards, longest-pattern-wins
// — so policy matching can never drift from route matching. A method
// mismatch or an unregistered path yields a non-sentinel handler and
// falls to the operator default (this also means a wrong-method request
// on a public route answers the gate's 401 rather than a 405 under
// `enabled` — fail closed, and no route-existence oracle).
var policyMux = newPolicyMux()

func newPolicyMux() *http.ServeMux {
	m := http.NewServeMux()
	reg := func(pattern string, p routePolicy) {
		m.Handle(pattern, policyHandler{policy: p})
	}

	// Identity-free surfaces.
	reg("GET /healthz", policyPublic)
	reg("POST /api/v1/auth/login", policyPublic)
	reg("GET /ui", policyPublic)
	reg("GET /ui/", policyPublic)
	// The console boot endpoints stay public: the SPA calls them before
	// any login form could have run (RFC 0048).
	reg("GET /api/v1/ui/config", policyPublic)
	reg("GET /api/v1/ui/context", policyPublic)

	// Agent-attributable REST ingress (§Non-Goals — RFC 0009 track).
	reg("POST /api/v1/agents/register", policyPublic)
	reg("DELETE /api/v1/agents/{id}", policyPublic) // self-deregistration on shutdown
	reg("GET /api/v1/channels", policyPublic)       // channel_catchup list
	reg("GET /api/v1/channels/{id}", policyPublic)  // channel_catchup members
	reg("GET /api/v1/channels/{id}/messages", policyPublic)
	reg("POST /api/v1/channels/{id}/messages", policyPublic) // HTTPChannelPublisher
	reg("POST /api/v1/channels/{id}/convene", policyPublic)  // convene timer callback

	// The authenticated human surface.
	reg("POST /api/v1/auth/logout", policyAuthenticated)
	reg("GET /api/v1/auth/whoami", policyAuthenticated)
	reg("GET /api/v1/workflows", policyAuthenticated)
	reg("GET /api/v1/workflows/{id}/status", policyAuthenticated)
	reg("GET /api/v1/agents", policyAuthenticated)
	reg("GET /api/v1/agents/{id}", policyAuthenticated)
	reg("GET /api/v1/executions/{id}/logs", policyAuthenticated)
	reg("GET /api/v1/executions/{id}/logs/stream", policyAuthenticated)
	reg("GET /api/v1/cost/summary", policyAuthenticated)
	reg("POST /api/v1/agents/{id}/chat", policyAuthenticated) // §F verified claim
	reg("GET /api/v1/agents/{id}/chat/history", policyAuthenticated)
	reg("GET /api/v1/agents/{id}/interactions/closed", policyAuthenticated)
	reg("GET /api/v1/sessions", policyAuthenticated)
	reg("GET /api/v1/sessions/{id}", policyAuthenticated)
	reg("GET /api/v1/channels/{id}/activity", policyAuthenticated)
	reg("GET /api/v1/channels/{id}/messages/{msg_id}/thread", policyAuthenticated)
	reg("GET /api/v1/channels/{id}/members/{participant_id}/history", policyAuthenticated)
	reg("GET /api/v1/channels/{id}/config", policyAuthenticated)

	// Operator mutations. (Everything unregistered is operator too —
	// these lines are the explicit assignment, not the safety net.)
	reg("POST /api/v1/workflows/run", policyOperator)
	reg("DELETE /api/v1/workflows/{id}", policyOperator)
	reg("POST /api/v1/agents/{id}/unquarantine", policyOperator) // §H
	reg("POST /api/v1/sessions", policyOperator)
	reg("POST /api/v1/sessions/{id}/archive", policyOperator)
	reg("POST /api/v1/channels", policyOperator)
	reg("DELETE /api/v1/channels/{id}", policyOperator)
	reg("POST /api/v1/channels/{id}/members", policyOperator)
	reg("PATCH /api/v1/channels/{id}/members/{participant_id}", policyOperator)
	reg("DELETE /api/v1/channels/{id}/members/{participant_id}", policyOperator)
	reg("PATCH /api/v1/channels/{id}/config", policyOperator)
	reg("POST /api/v1/personas/{participant_id}/recall", policyOperator)

	return m
}

// policyForRequest resolves the §E policy for a request. Fail closed:
// anything the table does not name — unregistered path, method
// mismatch, ServeMux's internal redirect handlers — is operator.
func policyForRequest(r *http.Request) routePolicy {
	h, _ := policyMux.Handler(r)
	if ph, ok := h.(policyHandler); ok {
		return ph.policy
	}
	return policyOperator
}

// authEnforced reports whether the §E/§F Phase 2 behaviour is live —
// the auth subsystem wired AND auth.mode: enabled. Handlers branch on
// this for the §F claim (chat), the §H env-token supersession
// (unquarantine), and the /ui/context identity.
func (s *Server) authEnforced() bool {
	return s.auth != nil && s.auth.cfg.Mode == AuthModeEnabled
}
