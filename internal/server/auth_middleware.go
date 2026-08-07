package server

import (
	"context"
	"net"
	"net/http"
	"net/url"
	"strings"

	"github.com/mkhomutov/persatrix/internal/accounts"
	"github.com/mkhomutov/persatrix/internal/security"
)

// sessionCookieName is the §A1 cookie form of the RFC 0039 session
// token. The `__Host-` prefix binds it to this origin: browsers accept
// it only with `Secure`, `Path=/` and no `Domain`, so a subdomain or a
// path-scoped page can never plant one. Browsers treat
// http://localhost as a trustworthy origin, so the loopback dev path
// still works; the un-prefixed fallback the amendment documents stays
// unimplemented until a browser proves inconsistent (MT-AUTH-001
// browser leg, PR 6).
const sessionCookieName = "__Host-persatrix_session"

// Session transport values (§A1) — how a login's session token is
// presented back to the client, chosen explicitly by the caller.
const (
	transportBearer = "bearer"
	transportCookie = "cookie"
)

// authIdentityKey carries the resolved [authIdentity] in the request
// context — the RFC 0002 unexported-contextKey pattern, like
// requestIDKey.
const authIdentityKey contextKey = "auth_identity"

// authIdentity is the per-request resolved identity (§E). Handlers read
// it via [identityFrom]; only the middleware writes it.
type authIdentity struct {
	Authenticated bool
	AccountID     string
	Username      string
	Role          string
	ParticipantID string
	// Transport records which §A1 channel presented the credential —
	// the §A2 same-origin assertion applies to cookie-resolved writes
	// only.
	Transport string
}

// anonymousIdentity is the RFC 0039 §H anonymous `local` identity every
// request resolves to under `auth.mode: disabled` (and any request
// presenting no credential under `enabled`, until Phase 2 enforcement).
var anonymousIdentity = authIdentity{ParticipantID: "local"}

// identityFrom returns the request's resolved identity, defaulting to
// the anonymous identity when the middleware has not run (bare test
// servers).
func identityFrom(ctx context.Context) authIdentity {
	if id, ok := ctx.Value(authIdentityKey).(authIdentity); ok {
		return id
	}
	return anonymousIdentity
}

// authRuntime bundles what the auth surface needs at request time. One
// Server field instead of five keeps server.go inside its size cap.
type authRuntime struct {
	store         *accounts.Store
	authenticator accounts.Authenticator
	cfg           *AuthConfig
	// The §B login limiters — dedicated instances, never the agent
	// limiter (§B2: its cardinality budget is sized for agents, and a
	// username-rotating attacker must not be able to evict agent rings).
	perSource   *security.RateLimiter
	perUsername *security.RateLimiter
}

// WithAuth wires the RFC 0039 accounts/auth subsystem: the account +
// session store, the §I authenticator, and the parsed `auth:` config.
// Nil-safe like WithChannels — absent any piece, no auth route is
// registered and the middleware resolves everything anonymous.
func WithAuth(store *accounts.Store, authenticator accounts.Authenticator, cfg *AuthConfig) ServerOption {
	return func(s *Server) {
		if store == nil || authenticator == nil || cfg == nil {
			return
		}
		s.auth = &authRuntime{store: store, authenticator: authenticator, cfg: cfg}
	}
}

// initAuthLimiters builds the two §B login limiters. Called from New
// AFTER the option loop so the server's logger and auditor are settled
// regardless of option order (the limiters emit `rate_limit.violated`
// through the same audit chain as the agent limiter).
func (s *Server) initAuthLimiters() error {
	if s.auth == nil {
		return nil
	}
	build := func(lc AuthLimiterConfig) (*security.RateLimiter, error) {
		return security.NewRateLimiter(security.RateLimitConfig{
			CallsPerWindow:   lc.CallsPerWindow,
			WindowSeconds:    lc.WindowSeconds,
			MaxTrackedAgents: lc.MaxTracked,
			Enabled:          true,
			Logger:           s.logger,
			Auditor:          s.auditor,
		})
	}
	var err error
	if s.auth.perSource, err = build(s.auth.cfg.LoginPerSource); err != nil {
		return err
	}
	s.auth.perUsername, err = build(s.auth.cfg.LoginPerUsername)
	return err
}

// authMiddleware is the §E identity-resolution and enforcement layer,
// composed inside logging and around the root mux so every route —
// API, console, and /healthz alike — carries a resolved identity.
//
// Under the default `auth.mode: disabled` every request is the
// anonymous `local` identity with zero DB cost and NO policy is
// evaluated (§H: byte-for-byte pre-RFC behaviour). Under `enabled`,
// Phase 2 enforces the §E 401/403 matrix against the auth_policy.go
// table. The §A2 same-origin assertion is unchanged from Phase 1 —
// CSRF defence on cookie-resolved writes, not policy, checked before
// the policy gate so a cross-site write dies 403 even on a route the
// policy would admit.
func (s *Server) authMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		ident := anonymousIdentity
		if s.authEnforced() {
			if resolved, ok := s.auth.resolveRequest(r); ok {
				if resolved.Transport == transportCookie && !isReadMethod(r.Method) && !sameOriginAllowed(r) {
					writeError(w, "FORBIDDEN", "cross-origin cookie-authenticated request rejected", http.StatusForbidden)
					return
				}
				ident = resolved
			}
			if !s.enforcePolicy(w, r, ident) {
				return
			}
		}
		ctx := context.WithValue(r.Context(), authIdentityKey, ident)
		// ISSUE-0082 Part 2 (v0.3.14 PR 2): thread the resolved §F
		// participant as the request PRINCIPAL, so every dispatch
		// descending from this request emits `persatrix-principal` and
		// the persona partitions its memory by the authenticated human
		// rather than the shared `'local'` tenant. Stamped here — the
		// one place identity is resolved — so no dispatch origin can
		// leak by omission; a no-op for every unauthenticated caller
		// (the whole persona fleet) and under `auth.mode: disabled`.
		// See principal.go for why the predicate is Authenticated
		// rather than the mode, and for the origin enumeration.
		ctx = withRequestPrincipal(ctx, ident)
		next.ServeHTTP(w, r.WithContext(ctx))
	})
}

// enforcePolicy applies the §E matrix to one request, answering false
// after writing the refusal. 401 for a missing identity on any gated
// route; 403 (+ the `authz.denied` security-class audit record) for a
// known identity below the requirement — an unknown role holds no
// privilege, so it 403s on operator routes like any non-operator.
func (s *Server) enforcePolicy(w http.ResponseWriter, r *http.Request, ident authIdentity) bool {
	required := policyForRequest(r)
	switch {
	case required == policyPublic:
		return true
	case !ident.Authenticated:
		writeError(w, "UNAUTHORIZED", "authentication required", http.StatusUnauthorized)
		return false
	case required == policyAuthenticated || ident.Role == accounts.RoleOperator:
		return true
	}
	s.emitAudit(r.Context(), security.AuditEvent{
		EventType: security.AuditAuthzDenied,
		Action:    "authorize",
		Resource:  r.URL.Path,
		Detail: map[string]any{
			"method":   r.Method,
			"username": ident.Username,
			"role":     ident.Role,
			"required": string(required),
			"source":   clientIP(r, s.auth.cfg.TrustedProxies),
		},
	})
	writeError(w, "FORBIDDEN", "operator role required", http.StatusForbidden)
	return false
}

// resolveRequest maps a presented credential to an identity. Resolution
// order is fixed — bearer first, cookie second (§A1): a request
// presenting both uses the bearer token and ignores the cookie, so
// resolution never depends on header iteration order.
func (a *authRuntime) resolveRequest(r *http.Request) (authIdentity, bool) {
	token, transport, ok := presentedToken(r)
	if !ok {
		return anonymousIdentity, false
	}
	_, acct, err := a.store.ResolveSession(r.Context(), token)
	if err != nil {
		// Not found / expired / revoked / disabled all collapse to "no
		// identity" (§D: no probe may distinguish why a token died).
		return anonymousIdentity, false
	}
	return authIdentity{
		Authenticated: true,
		AccountID:     acct.ID,
		Username:      acct.Username,
		Role:          acct.Role,
		ParticipantID: acct.ParticipantID,
		Transport:     transport,
	}, true
}

// presentedToken extracts the session credential a request carries,
// bearer-first (§A1). Shared by the middleware and the logout handler
// (which revokes the presented token directly, mode-independent).
func presentedToken(r *http.Request) (token, transport string, ok bool) {
	if h := r.Header.Get("Authorization"); h != "" {
		const prefix = "Bearer "
		if strings.HasPrefix(h, prefix) && len(h) > len(prefix) {
			return h[len(prefix):], transportBearer, true
		}
		return "", "", false // malformed Authorization — never fall through to the cookie
	}
	if c, err := r.Cookie(sessionCookieName); err == nil && c.Value != "" {
		return c.Value, transportCookie, true
	}
	return "", "", false
}

// isReadMethod reports whether the method is exempt from the §A2
// same-origin assertion (safe methods per RFC 9110).
func isReadMethod(method string) bool {
	return method == http.MethodGet || method == http.MethodHead || method == http.MethodOptions
}

// sameOriginAllowed is the §A2 server-side CSRF assertion for
// cookie-authenticated writes: accept `Sec-Fetch-Site: same-origin`,
// or an `Origin` header whose host equals the server's own Host.
// Everything else — a foreign Origin, or neither header — is rejected;
// bearer callers (the CLI sends no Origin) never reach this check.
func sameOriginAllowed(r *http.Request) bool {
	if r.Header.Get("Sec-Fetch-Site") == "same-origin" {
		return true
	}
	origin := r.Header.Get("Origin")
	if origin == "" {
		return false
	}
	u, err := url.Parse(origin)
	if err != nil {
		return false
	}
	return u.Host != "" && u.Host == r.Host
}

// clientIP resolves the per-source limiter key (§B3). The TCP peer is
// the source unless it is a configured trusted proxy, in which case
// X-Forwarded-For is walked right-to-left past trusted hops to the
// first untrusted address — so behind an unconfigured proxy the
// limiter degrades to per-proxy (global), which initAuth WARNs about
// at startup rather than silently.
func clientIP(r *http.Request, trusted []*net.IPNet) string {
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		host = r.RemoteAddr
	}
	peer := net.ParseIP(host)
	if peer == nil || !ipTrusted(peer, trusted) {
		return host
	}
	hops := strings.Split(strings.Join(r.Header.Values("X-Forwarded-For"), ","), ",")
	for i := len(hops) - 1; i >= 0; i-- {
		hop := strings.TrimSpace(hops[i])
		if hop == "" {
			continue
		}
		ip := net.ParseIP(hop)
		if ip == nil {
			// Junk in XFF: still a stable throttle key, and attacker
			// junk must not fall back to a trusted-looking value.
			return hop
		}
		if !ipTrusted(ip, trusted) {
			return ip.String()
		}
	}
	return host // every hop trusted — throttle the nearest proxy
}

func ipTrusted(ip net.IP, trusted []*net.IPNet) bool {
	for _, n := range trusted {
		if n.Contains(ip) {
			return true
		}
	}
	return false
}

// routePolicy is the §E per-route access level. The policies form a
// total order: operator ⊃ authenticated ⊃ public. The per-route
// assignment lives in auth_policy.go (Phase 2 — policyForRequest).
type routePolicy string

const (
	policyPublic        routePolicy = "public"
	policyAuthenticated routePolicy = "authenticated"
	policyOperator      routePolicy = "operator"
)
