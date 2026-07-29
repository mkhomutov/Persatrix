package server

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"testing/fstest"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/accounts"
	"github.com/mkhomutov/persatrix/internal/channels"
	"github.com/mkhomutov/persatrix/internal/planner"
	"github.com/mkhomutov/persatrix/internal/registry"
	"github.com/mkhomutov/persatrix/internal/security"
	"github.com/mkhomutov/persatrix/internal/state"
)

// RFC 0039 PR 5 (Phase 2) — the §E 401/403 enforcement matrix, the
// per-route policy assignment (design-review OQ #6), the agent-ingress
// carve-out (§Non-Goals), the §F verified claim, and `authz.denied`.

// enforcedHarness bundles what Phase 2 tests reach for: the server plus
// the seams the §F claim tests drive (registry to register the agent,
// router/store to publish the simulated reply).
type enforcedHarness struct {
	srv      *Server
	accounts *accounts.Store
	registry *registry.InMemoryRegistry
	chStore  channels.ChannelStore
	router   *channels.ChannelRouter
}

// newEnforcedServer builds a Server with BOTH the auth subsystem
// (enabled unless cfg overrides) and the channels subsystem wired, plus
// two accounts: alice (operator) and bob (plain user).
func newEnforcedServer(t *testing.T, cfg *AuthConfig, opts ...ServerOption) enforcedHarness {
	t.Helper()
	if cfg == nil {
		cfg = DefaultAuthConfig()
		cfg.Mode = AuthModeEnabled
	}
	cfg.LoginPerSource = AuthLimiterConfig{CallsPerWindow: 1000, WindowSeconds: 60, MaxTracked: 100}
	cfg.LoginPerUsername = AuthLimiterConfig{CallsPerWindow: 1000, WindowSeconds: 60, MaxTracked: 100}
	cfg.Argon = authTestParams

	store, err := accounts.Open(filepath.Join(t.TempDir(), "accounts.db"))
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })
	for _, acct := range []struct{ username, role, participant string }{
		{"alice", accounts.RoleOperator, "alice-participant"},
		{"bob", accounts.RoleUser, "bob-participant"},
	} {
		hash, err := accounts.HashPassword("s3cret", authTestParams)
		require.NoError(t, err)
		_, err = store.CreateAccount(context.Background(), accounts.NewAccount{
			Username:      acct.username,
			PasswordHash:  hash,
			Role:          acct.role,
			ParticipantID: acct.participant,
		})
		require.NoError(t, err)
	}

	logger := zap.NewNop()
	chStore, err := channels.NewSQLiteStore(filepath.Join(t.TempDir(), "channels.db"),
		channels.SQLiteOptions{MaxChannels: 50, Logger: logger})
	require.NoError(t, err)
	t.Cleanup(func() { _ = chStore.Close() })
	router := channels.NewChannelRouter(chStore, channels.NoopDispatcher{}, logger, nil)

	reg := registry.NewInMemoryRegistry(logger)
	srvOpts := append([]ServerOption{
		WithAuth(store, accounts.NewPasswordAuthenticator(store, authTestParams), cfg),
		WithChannels(chStore, router),
	}, opts...)
	srv, err := New("127.0.0.1:0", t.TempDir(), state.NewInMemoryStore(logger),
		reg, planner.NewYAMLPlanner(logger), logger,
		srvOpts...)
	require.NoError(t, err)
	return enforcedHarness{srv: srv, accounts: store, registry: reg, chStore: chStore, router: router}
}

// bearerFor logs a user in over the wire and returns the session token.
func bearerFor(t *testing.T, h http.Handler, username string) string {
	t.Helper()
	rec := postLogin(t, h, `{"username":"`+username+`","password":"s3cret"}`, nil)
	require.Equal(t, http.StatusOK, rec.Code, rec.Body.String())
	return decodeLogin(t, rec).Token
}

func request(h http.Handler, method, path, token string) *httptest.ResponseRecorder {
	var body *strings.Reader
	if method == http.MethodPost || method == http.MethodPatch {
		body = strings.NewReader("{}")
	} else {
		body = strings.NewReader("")
	}
	req := httptest.NewRequest(method, path, body)
	req.Header.Set("Content-Type", "application/json")
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec
}

func TestEnforcementMatrix(t *testing.T) {
	h5 := newEnforcedServer(t, nil)
	srv := h5.srv
	h := srv.Handler()
	operator := bearerFor(t, h, "alice")
	user := bearerFor(t, h, "bob")

	cases := []struct {
		method, path string
		policy       routePolicy
	}{
		{http.MethodGet, "/healthz", policyPublic},
		{http.MethodGet, "/api/v1/workflows", policyAuthenticated},
		{http.MethodGet, "/api/v1/agents", policyAuthenticated},
		{http.MethodGet, "/api/v1/executions/some-id/logs", policyAuthenticated},
		{http.MethodGet, "/api/v1/cost/summary", policyAuthenticated},
		{http.MethodGet, "/api/v1/sessions", policyAuthenticated},
		// channels/{id}/config is asserted at the policy table only
		// (TestPolicyMapPresentAndFailClosed): its handler 403s on the
		// config_edit_enabled deployment toggle, which would be
		// indistinguishable from the gate's 403 here.
		{http.MethodGet, "/api/v1/channels/group:x/activity", policyAuthenticated},
		{http.MethodPost, "/api/v1/workflows/run", policyOperator},
		{http.MethodDelete, "/api/v1/workflows/wf-1", policyOperator},
		{http.MethodPost, "/api/v1/sessions", policyOperator},
		{http.MethodPost, "/api/v1/channels", policyOperator},
		{http.MethodDelete, "/api/v1/channels/group:x", policyOperator},
		{http.MethodPost, "/api/v1/personas/some-p/recall", policyOperator},
		{http.MethodGet, "/api/v1/brand-new-route", policyOperator}, // unmapped → fail closed
	}
	for _, tc := range cases {
		t.Run(tc.method+" "+tc.path, func(t *testing.T) {
			anon := request(h, tc.method, tc.path, "").Code
			asUser := request(h, tc.method, tc.path, user).Code
			asOp := request(h, tc.method, tc.path, operator).Code

			switch tc.policy {
			case policyPublic:
				assert.NotEqual(t, http.StatusUnauthorized, anon)
				assert.NotEqual(t, http.StatusForbidden, anon)
			case policyAuthenticated:
				assert.Equal(t, http.StatusUnauthorized, anon, "anonymous must 401")
				assert.NotEqual(t, http.StatusUnauthorized, asUser, "any valid session passes")
				assert.NotEqual(t, http.StatusForbidden, asUser)
			case policyOperator:
				assert.Equal(t, http.StatusUnauthorized, anon, "anonymous must 401")
				assert.Equal(t, http.StatusForbidden, asUser, "user role must 403")
			}
			// The operator passes the gate everywhere; whatever the
			// handler then answers, it is never the gate's 401/403.
			assert.NotEqual(t, http.StatusUnauthorized, asOp)
			assert.NotEqual(t, http.StatusForbidden, asOp)
		})
	}
}

func TestAgentIngressStaysOpenUnderEnabled(t *testing.T) {
	// §Non-Goals: agent-attributable REST ingress (self-registration,
	// self-deregistration, and the RFC 0011 channel HTTP seams the
	// persona fleet publishes/fetches through) follows the RFC 0009
	// track, NOT this RFC — gating it would break every deployed agent
	// the moment auth.mode flips to enabled. These stay anonymous-
	// reachable, defended by the RFC 0009 limiter/quarantine.
	h5 := newEnforcedServer(t, nil)
	srv := h5.srv
	h := srv.Handler()

	for _, tc := range []struct{ method, path string }{
		{http.MethodPost, "/api/v1/agents/register"},
		{http.MethodDelete, "/api/v1/agents/some-agent"},
		{http.MethodGet, "/api/v1/channels"},
		{http.MethodGet, "/api/v1/channels/group:x"},
		{http.MethodGet, "/api/v1/channels/group:x/messages"},
		{http.MethodPost, "/api/v1/channels/group:x/messages"},
	} {
		rec := request(h, tc.method, tc.path, "")
		assert.NotEqual(t, http.StatusUnauthorized, rec.Code, "%s %s must stay agent-reachable", tc.method, tc.path)
		assert.NotEqual(t, http.StatusForbidden, rec.Code, "%s %s must stay agent-reachable", tc.method, tc.path)
	}
	// Convene (the standing-timer agent callback) is also carve-out
	// public, but its handler answers its own 403 when the deployment's
	// config_edit_enabled toggle is off — assert only that the AUTH gate
	// (401) never fires.
	rec := request(h, http.MethodPost, "/api/v1/channels/group:x/convene", "")
	assert.NotEqual(t, http.StatusUnauthorized, rec.Code, "convene must stay agent-reachable")
}

func TestDisabledModeEnforcesNothing(t *testing.T) {
	// §H: the disabled-mode regression pin — no route gains a 401/403.
	cfg := DefaultAuthConfig() // mode: disabled
	h5 := newEnforcedServer(t, cfg)
	srv := h5.srv
	h := srv.Handler()

	for _, tc := range []struct{ method, path string }{
		{http.MethodGet, "/api/v1/workflows"},
		{http.MethodPost, "/api/v1/workflows/run"},
		{http.MethodGet, "/api/v1/agents"},
		{http.MethodPost, "/api/v1/sessions"},
		{http.MethodGet, "/api/v1/auth/whoami"},
	} {
		rec := request(h, tc.method, tc.path, "")
		assert.NotEqual(t, http.StatusUnauthorized, rec.Code, "%s %s must not 401 under disabled", tc.method, tc.path)
		assert.NotEqual(t, http.StatusForbidden, rec.Code, "%s %s must not 403 under disabled", tc.method, tc.path)
	}
}

func TestWhoamiRequiresAuthUnderEnabled(t *testing.T) {
	// PR 3 shipped whoami reporting the anonymous identity honestly;
	// the §E matrix (this PR) 401s an anonymous whoami under enabled.
	h5 := newEnforcedServer(t, nil)
	srv := h5.srv
	h := srv.Handler()
	assert.Equal(t, http.StatusUnauthorized, request(h, http.MethodGet, "/api/v1/auth/whoami", "").Code)

	token := bearerFor(t, h, "bob")
	rec := request(h, http.MethodGet, "/api/v1/auth/whoami", token)
	require.Equal(t, http.StatusOK, rec.Code)
	var who whoamiResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &who))
	assert.True(t, who.Authenticated)
	assert.Equal(t, "bob-participant", who.ParticipantID)
}

func TestForbiddenEmitsAuthzDenied(t *testing.T) {
	auditor := &recordingAuditor{}
	h5 := newEnforcedServer(t, nil, WithAuditLogger(auditor))
	h := h5.srv.Handler()
	user := bearerFor(t, h, "bob")

	// An anonymous 401 emits nothing — unauthenticated pokes are
	// unbounded noise; `authz.denied` records AUTHORIZATION failures.
	require.Equal(t, http.StatusUnauthorized, request(h, http.MethodPost, "/api/v1/workflows/run", "").Code)
	assert.Empty(t, eventsOfType(auditor.all(), security.AuditAuthzDenied))

	require.Equal(t, http.StatusForbidden, request(h, http.MethodPost, "/api/v1/workflows/run", user).Code)
	denied := eventsOfType(auditor.all(), security.AuditAuthzDenied)
	require.Len(t, denied, 1)
	ev := denied[0]
	assert.Equal(t, "/api/v1/workflows/run", ev.Resource)
	assert.Equal(t, "bob", ev.Detail["username"])
	assert.Equal(t, accounts.RoleUser, ev.Detail["role"])
	assert.Equal(t, string(policyOperator), ev.Detail["required"])
	assert.Equal(t, http.MethodPost, ev.Detail["method"])
}

func TestUIContextReflectsVerifiedIdentity(t *testing.T) {
	h5 := newEnforcedServer(t, nil,
		WithUI(fstest.MapFS{"index.html": &fstest.MapFile{Data: []byte("console")}}))
	h := h5.srv.Handler()

	// Anonymous boot keeps the degenerate local identity (the console
	// boots before any login could have happened).
	rec := request(h, http.MethodGet, "/api/v1/ui/context", "")
	require.Equal(t, http.StatusOK, rec.Code)
	var ctx uiContextResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &ctx))
	assert.False(t, ctx.Authenticated)
	assert.Equal(t, "local", ctx.Principal)

	// A cookie login upgrades the reported principal to the verified
	// participant — the App's `authenticated` gate then hides the
	// acting-as override (RFC 0048 amendment §E).
	loginRec := postLogin(t, h, `{"username":"alice","password":"s3cret","session_transport":"cookie"}`, nil)
	require.Equal(t, http.StatusOK, loginRec.Code)
	session := loginRec.Result().Cookies()[0]
	req := httptest.NewRequest(http.MethodGet, "/api/v1/ui/context", nil)
	req.AddCookie(session)
	out := httptest.NewRecorder()
	h.ServeHTTP(out, req)
	require.Equal(t, http.StatusOK, out.Code)
	require.NoError(t, json.Unmarshal(out.Body.Bytes(), &ctx))
	assert.True(t, ctx.Authenticated)
	assert.Equal(t, "alice-participant", ctx.Principal)
}
