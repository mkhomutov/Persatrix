package server

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/accounts"
	"github.com/mkhomutov/persatrix/internal/planner"
	"github.com/mkhomutov/persatrix/internal/registry"
	"github.com/mkhomutov/persatrix/internal/state"
)

// RFC 0039 PR 3 — the /api/v1/auth surface + §A1/§A2/§B amendment
// behaviour, exercised through Server.Handler() so the root-mux mount,
// the middleware composition, and the §B limiters are all real.

// authTestParams keeps the KDF cheap in tests; production cost comes
// from config.
var authTestParams = accounts.Params{MemoryKiB: 1024, Iterations: 1, Parallelism: 1}

// newAuthServer builds a Server with the auth subsystem wired at the
// given mode, one active account (alice / s3cret, participant
// alice-participant, role operator), and generous limiter caps unless
// the caller overrides cfg.
func newAuthServer(t *testing.T, cfg *AuthConfig) (*Server, *accounts.Store) {
	t.Helper()
	if cfg == nil {
		cfg = DefaultAuthConfig()
		cfg.Mode = AuthModeEnabled
	}
	// Tests hammer login; default to caps that never trip unless a
	// throttle test tightened them explicitly.
	if cfg.LoginPerSource == DefaultAuthConfig().LoginPerSource {
		cfg.LoginPerSource = AuthLimiterConfig{CallsPerWindow: 1000, WindowSeconds: 60, MaxTracked: 100}
	}
	if cfg.LoginPerUsername == DefaultAuthConfig().LoginPerUsername {
		cfg.LoginPerUsername = AuthLimiterConfig{CallsPerWindow: 1000, WindowSeconds: 60, MaxTracked: 100}
	}
	cfg.Argon = authTestParams

	store, err := accounts.Open(filepath.Join(t.TempDir(), "accounts.db"))
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })

	hash, err := accounts.HashPassword("s3cret", authTestParams)
	require.NoError(t, err)
	_, err = store.CreateAccount(context.Background(), accounts.NewAccount{
		Username:      "alice",
		PasswordHash:  hash,
		Role:          accounts.RoleOperator,
		ParticipantID: "alice-participant",
	})
	require.NoError(t, err)

	logger := zap.NewNop()
	srv, err := New("127.0.0.1:0", t.TempDir(), state.NewInMemoryStore(logger),
		registry.NewInMemoryRegistry(logger), planner.NewYAMLPlanner(logger), logger,
		WithAuth(store, accounts.NewPasswordAuthenticator(store, authTestParams), cfg))
	require.NoError(t, err)
	return srv, store
}

func postLogin(t *testing.T, h http.Handler, body string, hdr map[string]string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, "/api/v1/auth/login", strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	for k, v := range hdr {
		req.Header.Set(k, v)
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec
}

func decodeLogin(t *testing.T, rec *httptest.ResponseRecorder) loginResponse {
	t.Helper()
	var resp loginResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	return resp
}

func TestLoginBearerRoundTrip(t *testing.T) {
	srv, _ := newAuthServer(t, nil)
	h := srv.Handler()

	rec := postLogin(t, h, `{"username":"alice","password":"s3cret"}`, nil)
	require.Equal(t, http.StatusOK, rec.Code, rec.Body.String())
	resp := decodeLogin(t, rec)
	assert.NotEmpty(t, resp.Token, "bearer transport returns the token in the body")
	assert.Empty(t, rec.Result().Cookies(), "bearer transport sets no cookie")
	assert.Equal(t, "alice-participant", resp.ParticipantID)
	assert.Equal(t, accounts.RoleOperator, resp.Role)

	expires, err := time.Parse(time.RFC3339, resp.ExpiresAt)
	require.NoError(t, err)
	assert.InDelta(t, time.Until(expires).Hours(), 24, 0.1, "bearer TTL is session_ttl (24h)")

	// whoami with the bearer token reports the verified identity.
	req := httptest.NewRequest(http.MethodGet, "/api/v1/auth/whoami", nil)
	req.Header.Set("Authorization", "Bearer "+resp.Token)
	rec2 := httptest.NewRecorder()
	h.ServeHTTP(rec2, req)
	require.Equal(t, http.StatusOK, rec2.Code)
	var who whoamiResponse
	require.NoError(t, json.Unmarshal(rec2.Body.Bytes(), &who))
	assert.True(t, who.Authenticated)
	assert.Equal(t, "alice-participant", who.ParticipantID)
	assert.Equal(t, "alice", who.Username)
}

func TestLoginCookieTransport(t *testing.T) {
	srv, _ := newAuthServer(t, nil)
	h := srv.Handler()

	rec := postLogin(t, h, `{"username":"alice","password":"s3cret","session_transport":"cookie"}`, nil)
	require.Equal(t, http.StatusOK, rec.Code, rec.Body.String())
	resp := decodeLogin(t, rec)
	assert.Empty(t, resp.Token, "cookie transport returns NO body token (§A1)")

	cookies := rec.Result().Cookies()
	require.Len(t, cookies, 1)
	c := cookies[0]
	assert.Equal(t, sessionCookieName, c.Name)
	assert.NotEmpty(t, c.Value)
	assert.True(t, c.HttpOnly, "HttpOnly — the token never enters JS")
	assert.True(t, c.Secure, "__Host- requires Secure")
	assert.Equal(t, "/", c.Path, "__Host- requires Path=/")
	assert.Equal(t, http.SameSiteStrictMode, c.SameSite)
	assert.Empty(t, c.Domain, "__Host- forbids Domain")
	// OQ #2: the cookie TTL is the shorter cookie_session_ttl (8h).
	assert.InDelta(t, 8*3600, c.MaxAge, 5)

	expires, err := time.Parse(time.RFC3339, resp.ExpiresAt)
	require.NoError(t, err)
	assert.InDelta(t, time.Until(expires).Hours(), 8, 0.1)
}

func TestLoginTransportValidation(t *testing.T) {
	srv, _ := newAuthServer(t, nil)
	h := srv.Handler()
	rec := postLogin(t, h, `{"username":"alice","password":"s3cret","session_transport":"header"}`, nil)
	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

func TestLoginFailuresAreIndistinguishable(t *testing.T) {
	srv, store := newAuthServer(t, nil)
	h := srv.Handler()

	// A disabled account with the CORRECT password must answer exactly
	// like a wrong password and an unknown username (§C).
	hash, err := accounts.HashPassword("otherpw", authTestParams)
	require.NoError(t, err)
	disabled, err := store.CreateAccount(context.Background(), accounts.NewAccount{
		Username: "mallory", PasswordHash: hash, Role: accounts.RoleUser, ParticipantID: "mallory-p",
	})
	require.NoError(t, err)
	require.NoError(t, store.SetAccountStatus(context.Background(), disabled.ID, accounts.StatusDisabled))

	wrongPw := postLogin(t, h, `{"username":"alice","password":"nope"}`, nil)
	unknown := postLogin(t, h, `{"username":"nobody","password":"nope"}`, nil)
	disabledOK := postLogin(t, h, `{"username":"mallory","password":"otherpw"}`, nil)

	for _, rec := range []*httptest.ResponseRecorder{wrongPw, unknown, disabledOK} {
		assert.Equal(t, http.StatusUnauthorized, rec.Code)
	}
	assert.Equal(t, wrongPw.Body.String(), unknown.Body.String())
	assert.Equal(t, wrongPw.Body.String(), disabledOK.Body.String())
}

func TestLogoutBearerRevokesServerSide(t *testing.T) {
	srv, _ := newAuthServer(t, nil)
	h := srv.Handler()
	token := decodeLogin(t, postLogin(t, h, `{"username":"alice","password":"s3cret"}`, nil)).Token

	// Bearer-authenticated POST with no Origin header must pass — the
	// CLI regression pinned by the amendment's test strategy.
	req := httptest.NewRequest(http.MethodPost, "/api/v1/auth/logout", nil)
	req.Header.Set("Authorization", "Bearer "+token)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	require.Equal(t, http.StatusNoContent, rec.Code)

	// The revocation is server-side: the token no longer resolves.
	req2 := httptest.NewRequest(http.MethodGet, "/api/v1/auth/whoami", nil)
	req2.Header.Set("Authorization", "Bearer "+token)
	rec2 := httptest.NewRecorder()
	h.ServeHTTP(rec2, req2)
	var who whoamiResponse
	require.NoError(t, json.Unmarshal(rec2.Body.Bytes(), &who))
	assert.False(t, who.Authenticated)
}

func TestLogoutWithoutSessionIs401(t *testing.T) {
	srv, _ := newAuthServer(t, nil)
	rec := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/api/v1/auth/logout", nil))
	assert.Equal(t, http.StatusUnauthorized, rec.Code)
}

func TestLogoutCookieClearsAndRequiresSameOrigin(t *testing.T) {
	srv, _ := newAuthServer(t, nil)
	h := srv.Handler()
	loginRec := postLogin(t, h, `{"username":"alice","password":"s3cret","session_transport":"cookie"}`, nil)
	require.Equal(t, http.StatusOK, loginRec.Code)
	session := loginRec.Result().Cookies()[0]

	// §A2 CSRF matrix on the one cookie-write this PR ships (logout).
	post := func(hdr map[string]string) *httptest.ResponseRecorder {
		req := httptest.NewRequest(http.MethodPost, "/api/v1/auth/logout", nil)
		req.AddCookie(session)
		for k, v := range hdr {
			req.Header.Set(k, v)
		}
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, req)
		return rec
	}

	assert.Equal(t, http.StatusForbidden, post(nil).Code,
		"cookie write with neither Sec-Fetch-Site nor Origin is rejected")
	assert.Equal(t, http.StatusForbidden, post(map[string]string{"Origin": "https://evil.example"}).Code,
		"foreign Origin is rejected")

	ok := post(map[string]string{"Sec-Fetch-Site": "same-origin"})
	require.Equal(t, http.StatusNoContent, ok.Code)
	cleared := ok.Result().Cookies()
	require.Len(t, cleared, 1)
	assert.Empty(t, cleared[0].Value)
	assert.Less(t, cleared[0].MaxAge, 0, "logout clears the cookie (Max-Age=0 on the wire)")
}

func TestCookieWriteAllowedWithMatchingOrigin(t *testing.T) {
	srv, _ := newAuthServer(t, nil)
	h := srv.Handler()
	loginRec := postLogin(t, h, `{"username":"alice","password":"s3cret","session_transport":"cookie"}`, nil)
	session := loginRec.Result().Cookies()[0]

	req := httptest.NewRequest(http.MethodPost, "http://example.com/api/v1/auth/logout", nil)
	req.AddCookie(session)
	req.Header.Set("Origin", "http://example.com")
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	assert.Equal(t, http.StatusNoContent, rec.Code, "matching Origin host passes the §A2 assertion")
}

func TestLoginThrottlePerSource(t *testing.T) {
	cfg := DefaultAuthConfig()
	cfg.Mode = AuthModeEnabled
	cfg.LoginPerSource = AuthLimiterConfig{CallsPerWindow: 3, WindowSeconds: 60, MaxTracked: 10}
	cfg.LoginPerUsername = AuthLimiterConfig{CallsPerWindow: 1000, WindowSeconds: 60, MaxTracked: 100}
	srv, _ := newAuthServer(t, cfg)
	h := srv.Handler()

	for i := 0; i < 3; i++ {
		rec := postLogin(t, h, `{"username":"alice","password":"nope"}`, nil)
		require.Equal(t, http.StatusUnauthorized, rec.Code, "attempt %d within budget", i)
	}
	// The 4th attempt trips the source limiter BEFORE any KDF work; the
	// 429 is identical whether the username exists or not (§B4).
	trippedKnown := postLogin(t, h, `{"username":"alice","password":"nope"}`, nil)
	require.Equal(t, http.StatusTooManyRequests, trippedKnown.Code)
	assert.Equal(t, "60", trippedKnown.Header().Get("Retry-After"))
	trippedUnknown := postLogin(t, h, `{"username":"ghost","password":"nope"}`, nil)
	require.Equal(t, http.StatusTooManyRequests, trippedUnknown.Code)
	assert.Equal(t, trippedKnown.Body.String(), trippedUnknown.Body.String())
	assert.Equal(t, trippedKnown.Header().Get("Retry-After"), trippedUnknown.Header().Get("Retry-After"))
}

func TestLoginThrottlePerUsername(t *testing.T) {
	cfg := DefaultAuthConfig()
	cfg.Mode = AuthModeEnabled
	cfg.LoginPerSource = AuthLimiterConfig{CallsPerWindow: 1000, WindowSeconds: 60, MaxTracked: 100}
	cfg.LoginPerUsername = AuthLimiterConfig{CallsPerWindow: 2, WindowSeconds: 30, MaxTracked: 10}
	srv, _ := newAuthServer(t, cfg)
	h := srv.Handler()

	for i := 0; i < 2; i++ {
		require.Equal(t, http.StatusUnauthorized,
			postLogin(t, h, `{"username":"Alice","password":"nope"}`, nil).Code)
	}
	// Case-folded key: "ALICE" shares "Alice"'s budget.
	rec := postLogin(t, h, `{"username":"ALICE","password":"nope"}`, nil)
	require.Equal(t, http.StatusTooManyRequests, rec.Code)
	assert.Equal(t, "30", rec.Header().Get("Retry-After"))
	// A different username still has budget — the key is the username.
	assert.Equal(t, http.StatusUnauthorized,
		postLogin(t, h, `{"username":"bob","password":"nope"}`, nil).Code)
}

func TestLoginThrottleLiveUnderDisabledMode(t *testing.T) {
	// §B5: the throttle ships with the endpoint, under BOTH modes — the
	// route did not exist before this PR, so inertness holds.
	cfg := DefaultAuthConfig()
	cfg.LoginPerSource = AuthLimiterConfig{CallsPerWindow: 1, WindowSeconds: 60, MaxTracked: 10}
	cfg.LoginPerUsername = AuthLimiterConfig{CallsPerWindow: 1000, WindowSeconds: 60, MaxTracked: 100}
	srv, _ := newAuthServer(t, cfg)
	h := srv.Handler()

	require.Equal(t, http.StatusOK,
		postLogin(t, h, `{"username":"alice","password":"s3cret"}`, nil).Code,
		"login functions under auth.mode: disabled — Phase 1 ships the mechanism inert, not absent")
	assert.Equal(t, http.StatusTooManyRequests,
		postLogin(t, h, `{"username":"alice","password":"s3cret"}`, nil).Code)
}

func TestAuthRoutesAbsentWhenUnwired(t *testing.T) {
	srv, _ := testServer(t)
	rec := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/api/v1/auth/whoami", nil))
	assert.Equal(t, http.StatusNotFound, rec.Code, "no WithAuth → no auth route registers")
}

func TestLoginRequiresJSONContentType(t *testing.T) {
	srv, _ := newAuthServer(t, nil)
	req := httptest.NewRequest(http.MethodPost, "/api/v1/auth/login", strings.NewReader("u=alice"))
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	rec := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, req)
	assert.Equal(t, http.StatusBadRequest, rec.Code)
}
