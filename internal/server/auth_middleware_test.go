package server

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// RFC 0039 PR 3 — §E identity resolution, §A1 transport precedence,
// §A2 same-origin assertion internals, §B3 client-IP resolution, and
// the §E policy map (present, unenforced until Phase 2).

func whoami(t *testing.T, h http.Handler, mutate func(*http.Request)) whoamiResponse {
	t.Helper()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/auth/whoami", nil)
	if mutate != nil {
		mutate(req)
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	require.Equal(t, http.StatusOK, rec.Code)
	var who whoamiResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &who))
	return who
}

func TestDisabledModeResolvesEverythingAnonymous(t *testing.T) {
	// §H: under `disabled`, even a VALID token resolves to the anonymous
	// `local` identity with no session lookup — behaviour (and DB cost)
	// is byte-for-byte pre-RFC-0039.
	cfg := DefaultAuthConfig() // mode: disabled
	srv, store := newAuthServer(t, cfg)
	h := srv.Handler()

	acct, err := store.GetAccountByUsername(context.Background(), "alice")
	require.NoError(t, err)
	token, _, err := store.IssueSession(context.Background(), acct.ID, cfg.SessionTTL)
	require.NoError(t, err)

	who := whoami(t, h, func(r *http.Request) { r.Header.Set("Authorization", "Bearer "+token) })
	assert.False(t, who.Authenticated)
	assert.Equal(t, "local", who.ParticipantID)
	assert.Empty(t, who.Role)
}

func TestAnonymousIdentityIsLocal(t *testing.T) {
	srv, _ := newAuthServer(t, nil) // enabled
	who := whoami(t, srv.Handler(), nil)
	assert.False(t, who.Authenticated)
	assert.Equal(t, "local", who.ParticipantID)
}

func TestInvalidTokenResolvesAnonymousNotRejected(t *testing.T) {
	// Phase 1 is non-enforcing: a dead token means "no identity", never
	// a 401 — the §E matrix lands in Phase 2 (PR 5).
	srv, _ := newAuthServer(t, nil)
	who := whoami(t, srv.Handler(), func(r *http.Request) {
		r.Header.Set("Authorization", "Bearer not-a-real-token")
	})
	assert.False(t, who.Authenticated)
}

func TestBearerTakesPrecedenceOverCookie(t *testing.T) {
	// §A1: presenting both uses the bearer token and IGNORES the cookie
	// — here the bearer is dead and the cookie live, so resolution must
	// fail rather than fall through to the cookie.
	srv, _ := newAuthServer(t, nil)
	h := srv.Handler()
	loginRec := postLogin(t, h, `{"username":"alice","password":"s3cret","session_transport":"cookie"}`, nil)
	require.Equal(t, http.StatusOK, loginRec.Code)
	session := loginRec.Result().Cookies()[0]

	who := whoami(t, h, func(r *http.Request) {
		r.Header.Set("Authorization", "Bearer dead-token")
		r.AddCookie(session)
	})
	assert.False(t, who.Authenticated, "dead bearer must not fall through to the live cookie")

	// Cookie alone resolves (read method — no §A2 assertion).
	whoCookie := whoami(t, h, func(r *http.Request) { r.AddCookie(session) })
	assert.True(t, whoCookie.Authenticated)
}

func TestMalformedAuthorizationNeverFallsThrough(t *testing.T) {
	srv, _ := newAuthServer(t, nil)
	h := srv.Handler()
	loginRec := postLogin(t, h, `{"username":"alice","password":"s3cret","session_transport":"cookie"}`, nil)
	session := loginRec.Result().Cookies()[0]

	who := whoami(t, h, func(r *http.Request) {
		r.Header.Set("Authorization", "Basic dXNlcjpwdw==")
		r.AddCookie(session)
	})
	assert.False(t, who.Authenticated)
}

func TestSameOriginAllowed(t *testing.T) {
	mk := func(hdr map[string]string) *http.Request {
		r := httptest.NewRequest(http.MethodPost, "http://console.local/api/v1/auth/logout", nil)
		for k, v := range hdr {
			r.Header.Set(k, v)
		}
		return r
	}
	assert.True(t, sameOriginAllowed(mk(map[string]string{"Sec-Fetch-Site": "same-origin"})))
	assert.True(t, sameOriginAllowed(mk(map[string]string{"Origin": "http://console.local"})))
	assert.False(t, sameOriginAllowed(mk(map[string]string{"Origin": "http://evil.example"})))
	assert.False(t, sameOriginAllowed(mk(map[string]string{"Sec-Fetch-Site": "cross-site"})))
	assert.False(t, sameOriginAllowed(mk(nil)), "neither header → rejected")
	assert.False(t, sameOriginAllowed(mk(map[string]string{"Origin": "null"})),
		"an opaque `null` Origin (sandboxed iframe / data: page) is cross-origin")
}

func TestIsReadMethod(t *testing.T) {
	assert.True(t, isReadMethod(http.MethodGet))
	assert.True(t, isReadMethod(http.MethodHead))
	assert.True(t, isReadMethod(http.MethodOptions))
	assert.False(t, isReadMethod(http.MethodPost))
	assert.False(t, isReadMethod(http.MethodDelete))
	assert.False(t, isReadMethod(http.MethodPatch))
}

func TestClientIPResolution(t *testing.T) {
	cfgProxies, err := LoadSecurityConfig(writeSecurityYAML(t,
		"auth:\n  trusted_proxies: [\"10.0.0.0/8\"]\n"))
	require.NoError(t, err)
	trusted := cfgProxies.TrustedProxies

	mk := func(remote, xff string) *http.Request {
		r := httptest.NewRequest(http.MethodPost, "/api/v1/auth/login", nil)
		r.RemoteAddr = remote
		if xff != "" {
			r.Header.Set("X-Forwarded-For", xff)
		}
		return r
	}

	// Direct bind: the TCP peer is the source; XFF from an untrusted
	// peer is attacker-controlled and ignored.
	assert.Equal(t, "203.0.113.9", clientIP(mk("203.0.113.9:1234", "1.2.3.4"), trusted))
	assert.Equal(t, "203.0.113.9", clientIP(mk("203.0.113.9:1234", ""), nil))

	// Trusted proxy: walk XFF right-to-left past trusted hops to the
	// first untrusted address (§B3).
	assert.Equal(t, "198.51.100.7", clientIP(mk("10.0.0.1:9999", "198.51.100.7"), trusted))
	assert.Equal(t, "198.51.100.7", clientIP(mk("10.0.0.1:9999", "6.6.6.6, 198.51.100.7, 10.0.0.2"), trusted))

	// Every hop trusted → throttle the nearest proxy, never a spoofable
	// deeper value.
	assert.Equal(t, "10.0.0.1", clientIP(mk("10.0.0.1:9999", "10.0.0.3, 10.0.0.2"), trusted))

	// Junk in XFF stays a stable throttle key rather than being skipped
	// toward something attacker-chosen.
	assert.Equal(t, "junk-value", clientIP(mk("10.0.0.1:9999", "198.51.100.7, junk-value"), trusted))
}

func TestPolicyMapPresentAndFailClosed(t *testing.T) {
	// §E: the map ships with the middleware; enforcement is Phase 2. A
	// route absent from the map is `operator` — fail closed.
	assert.Equal(t, policyPublic, policyFor("/healthz"))
	assert.Equal(t, policyPublic, policyFor("/api/v1/auth/login"))
	assert.Equal(t, policyPublic, policyFor("/ui/"))
	assert.Equal(t, policyPublic, policyFor("/ui/index.html"))
	assert.Equal(t, policyPublic, policyFor("/api/v1/ui/config"))
	assert.Equal(t, policyPublic, policyFor("/api/v1/ui/context"))
	assert.Equal(t, policyAuthenticated, policyFor("/api/v1/auth/logout"))
	assert.Equal(t, policyAuthenticated, policyFor("/api/v1/auth/whoami"))
	assert.Equal(t, policyOperator, policyFor("/api/v1/agents"), "unmapped → operator")
	assert.Equal(t, policyOperator, policyFor("/api/v1/brand-new-route"), "unknown → operator (fail closed)")
}
