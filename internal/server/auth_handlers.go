package server

import (
	"errors"
	"net/http"
	"strconv"
	"time"

	"github.com/mkhomutov/persatrix/internal/accounts"
	"github.com/mkhomutov/persatrix/internal/security"
)

// Auth endpoints (RFC 0039 §K Phase 1 + the enabled-mode exposure
// amendment §A1/§B): POST /api/v1/auth/login, POST /api/v1/auth/logout,
// GET /api/v1/auth/whoami.
//
// They register on the security-bypass ROOT mux (beside /healthz and
// the RFC 0048 console), NOT behind RESTRateLimitMiddleware: login is
// anonymous human traffic, and the PR #244 H-01 anonymous-deny fires
// whenever any agent is quarantined — routing login through that layer
// would lock operators out exactly when they need a session to
// investigate (the registerUIRoutes argument, verbatim). The surface is
// instead defended by its own §B limiters, which unlike the agent
// limiter are sized for and keyed to login attempts.

// loginRequest is the §K login body. `session_transport` (§A1) chooses
// how the session comes back — explicit beats sniffing Origin to guess
// "is this a browser": a request parameter is testable and cannot
// drift with client header changes.
type loginRequest struct {
	Username         string `json:"username"`
	Password         string `json:"password"`
	SessionTransport string `json:"session_transport,omitempty"`
}

// loginResponse is the §D login success shape. Token is present only
// under the bearer transport — the cookie transport's whole point is
// that the token never enters page-readable space (§A1).
type loginResponse struct {
	Token         string `json:"token,omitempty"`
	ExpiresAt     string `json:"expires_at"`
	ParticipantID string `json:"participant_id"`
	Role          string `json:"role"`
}

// whoamiResponse reports the request's resolved identity (§K). Under
// `auth.mode: disabled` — or an anonymous request pre-enforcement —
// that is the §H anonymous `local` identity, reported honestly rather
// than 401'd (enforcement is Phase 2).
type whoamiResponse struct {
	Authenticated bool   `json:"authenticated"`
	ParticipantID string `json:"participant_id"`
	Role          string `json:"role,omitempty"`
	Username      string `json:"username,omitempty"`
	AccountID     string `json:"account_id,omitempty"`
}

// registerAuthRoutes mounts the auth endpoints on the root mux when the
// subsystem is wired (WithAuth); absent it, no route registers and the
// paths 404 — the registerUIRoutes nil-gating pattern.
func (s *Server) registerAuthRoutes(mux *http.ServeMux) {
	if s.auth == nil {
		return
	}
	mux.HandleFunc("POST /api/v1/auth/login", s.handleAuthLogin)
	mux.HandleFunc("POST /api/v1/auth/logout", s.handleAuthLogout)
	mux.HandleFunc("GET /api/v1/auth/whoami", s.handleAuthWhoami)
}

// handleAuthLogin verifies a credential and issues a session (§C/§D).
//
// Ordering is load-bearing: both §B limiters are consulted BEFORE any
// KDF work, because every failed verification burns a full Argon2id
// hash (the §C account-existence non-disclosure hashes a dummy on
// unknown usernames) — login is a CPU amplification vector for an
// unauthenticated caller, and the throttle is what bounds it. The
// throttle is live under BOTH auth modes (§B5): the route did not
// exist before this PR, so no pre-existing behaviour changes and the
// Phase-1 inertness contract holds.
func (s *Server) handleAuthLogin(w http.ResponseWriter, r *http.Request) {
	if !requireJSON(w, r) {
		return
	}
	var req loginRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	transport := req.SessionTransport
	if transport == "" {
		transport = transportBearer
	}
	if transport != transportBearer && transport != transportCookie {
		writeError(w, "BAD_REQUEST", "session_transport must be \"bearer\" or \"cookie\"", http.StatusBadRequest)
		return
	}

	// Per-source first and short-circuiting: a flooding source burns its
	// own budget without poisoning the username key it happens to spray.
	source := clientIP(r, s.auth.cfg.TrustedProxies)
	if !s.auth.perSource.Allow(r.Context(), source) {
		s.writeLoginThrottled(w, s.auth.cfg.LoginPerSource.WindowSeconds)
		return
	}
	if !s.auth.perUsername.Allow(r.Context(), usernameThrottleKey(req.Username)) {
		s.writeLoginThrottled(w, s.auth.cfg.LoginPerUsername.WindowSeconds)
		return
	}

	accountID, err := s.auth.authenticator.Authenticate(r.Context(), accounts.Credentials{
		Username: req.Username,
		Password: req.Password,
	})
	if err != nil {
		// Invalid credential and disabled account answer identically —
		// a disabled-account distinction would confirm both existence
		// and password correctness to whoever holds the password (§C).
		// The AUDIT record keeps the true reason: it is operator-side,
		// and a disabled account being probed is a signal (step 10).
		if errors.Is(err, accounts.ErrInvalidCredentials) || errors.Is(err, accounts.ErrAccountDisabled) {
			reason := "invalid_credentials"
			if errors.Is(err, accounts.ErrAccountDisabled) {
				reason = "account_disabled"
			}
			s.emitAudit(r.Context(), security.AuditEvent{
				EventType: security.AuditAuthLoginFailed,
				Action:    "login",
				Detail: map[string]any{
					"username": accounts.FoldUsername(req.Username),
					"source":   source,
					"reason":   reason,
				},
			})
			writeError(w, "UNAUTHORIZED", "invalid credentials", http.StatusUnauthorized)
			return
		}
		writeError(w, "INTERNAL", "login failed", http.StatusInternalServerError)
		return
	}

	ttl := s.auth.cfg.SessionTTL
	if transport == transportCookie {
		ttl = s.auth.cfg.CookieSessionTTL
	}
	token, sess, err := s.auth.store.IssueSession(r.Context(), accountID, ttl)
	if err != nil {
		writeError(w, "INTERNAL", "login failed", http.StatusInternalServerError)
		return
	}
	acct, err := s.auth.store.GetAccount(r.Context(), accountID)
	if err != nil {
		writeError(w, "INTERNAL", "login failed", http.StatusInternalServerError)
		return
	}
	s.emitAudit(r.Context(), security.AuditEvent{
		EventType: security.AuditAuthLoginSucceeded,
		Action:    "login",
		Resource:  acct.ID,
		Detail: map[string]any{
			"username":  acct.Username,
			"role":      acct.Role,
			"transport": transport,
			"source":    source,
		},
	})

	resp := loginResponse{
		ExpiresAt:     sess.ExpiresAt.Format(time.RFC3339),
		ParticipantID: acct.ParticipantID,
		Role:          acct.Role,
	}
	if transport == transportBearer {
		resp.Token = token
	} else {
		http.SetCookie(w, sessionCookie(token, int(ttl.Seconds())))
	}
	writeJSON(w, resp, http.StatusOK)
}

// usernameThrottleKey resolves the §B per-username limiter key. An
// empty (or all-whitespace) username folds to "", which the limiter
// would resolve to its shared "anonymous" bucket — a bucket that emits
// the agent-surface `rate_limit.unauthenticated_caller` security-class
// (fsync'd) audit event on EVERY call, unthrottled. The sentinel keeps
// empty-username probes in an ordinary tracked bucket instead. It can
// never collide with a real account: validateUsername rejects
// whitespace, so no stored username contains a space.
func usernameThrottleKey(username string) string {
	if folded := accounts.FoldUsername(username); folded != "" {
		return folded
	}
	return "(empty username)"
}

// writeLoginThrottled answers a limiter denial. The 429 is IDENTICAL
// whichever limiter tripped and whether or not the username exists —
// throttling must not become an account-existence oracle (§B4).
func (s *Server) writeLoginThrottled(w http.ResponseWriter, windowSeconds int) {
	w.Header().Set("Retry-After", strconv.Itoa(windowSeconds))
	writeError(w, "RATE_LIMITED", "too many login attempts", http.StatusTooManyRequests)
}

// sessionCookie builds the §A1 cookie: __Host- prefixed, HttpOnly (the
// token never enters JS), Secure, SameSite=Strict, Path=/. maxAge ≤ 0
// clears it (logout).
func sessionCookie(value string, maxAge int) *http.Cookie {
	if maxAge <= 0 {
		maxAge = -1
	}
	return &http.Cookie{
		Name:     sessionCookieName,
		Value:    value,
		Path:     "/",
		MaxAge:   maxAge,
		Secure:   true,
		HttpOnly: true,
		SameSite: http.SameSiteStrictMode,
	}
}

// handleAuthLogout revokes the PRESENTED session (§D) — read straight
// off the request, not the middleware identity, so logout works under
// both auth modes (under `disabled` the middleware resolves nothing,
// but a session issued by login must still be revocable). Server-side
// revocation is the logout; the cookie clear is hygiene on top (§A4 —
// client-side clearing alone is never logout).
func (s *Server) handleAuthLogout(w http.ResponseWriter, r *http.Request) {
	token, transport, ok := presentedToken(r)
	if !ok {
		writeError(w, "UNAUTHORIZED", "no session presented", http.StatusUnauthorized)
		return
	}
	// Resolve BEFORE revoking, purely to name the account in the audit
	// record — revocation itself never depends on this succeeding (an
	// expired session must still be revocable, and still audits).
	var acct *accounts.Account
	if _, resolved, err := s.auth.store.ResolveSession(r.Context(), token); err == nil {
		acct = resolved
	}
	if err := s.auth.store.RevokeSession(r.Context(), token); err != nil {
		// Unknown/pruned tokens answer the same 401 as no token — no
		// probe may distinguish why a token died (§D).
		writeError(w, "UNAUTHORIZED", "no session presented", http.StatusUnauthorized)
		return
	}
	ev := security.AuditEvent{
		EventType: security.AuditAuthLogout,
		Action:    "logout",
		Detail: map[string]any{
			"transport": transport,
			"source":    clientIP(r, s.auth.cfg.TrustedProxies),
		},
	}
	if acct != nil {
		ev.Resource = acct.ID
		ev.Detail["username"] = acct.Username
	}
	s.emitAudit(r.Context(), ev)
	if transport == transportCookie {
		http.SetCookie(w, sessionCookie("", 0))
	}
	w.WriteHeader(http.StatusNoContent)
}

// handleAuthWhoami reports the middleware-resolved identity (§K).
func (s *Server) handleAuthWhoami(w http.ResponseWriter, r *http.Request) {
	ident := identityFrom(r.Context())
	writeJSON(w, whoamiResponse{
		Authenticated: ident.Authenticated,
		ParticipantID: ident.ParticipantID,
		Role:          ident.Role,
		Username:      ident.Username,
		AccountID:     ident.AccountID,
	}, http.StatusOK)
}
