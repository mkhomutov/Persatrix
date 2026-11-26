package server

// request_cross_origin_write_test.go: the second of the two checks of where
// a request came from (ISSUE-0212). A write that the browser marks as coming
// from another origin is refused, whether sign-in is on or off, and a client
// that is not a browser is left alone. The Host check, and the helpers both
// files use, are in request_origin_test.go.

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
	"go.uber.org/zap/zapcore"
	"go.uber.org/zap/zaptest/observer"

	"github.com/mkhomutov/persatrix/internal/channels"
)

const crossOriginRefusalText = "cross-origin write refused: the browser reports that this request comes from another origin"

func TestOriginChecks_CrossOriginWrites(t *testing.T) {
	const host = "localhost:8080"
	tests := []struct {
		name    string
		method  string
		headers map[string]string
		refused bool
	}{
		// Every client that is not a browser: the CLI, the agents, curl.
		{name: "neither header", method: http.MethodPost},
		{name: "the browser says same-origin", method: http.MethodPost, headers: map[string]string{"Sec-Fetch-Site": sameOrigin}},
		{name: "the browser says the user asked for it", method: http.MethodPost, headers: map[string]string{"Sec-Fetch-Site": "none"}},
		{name: "same-origin is believed over an Origin", method: http.MethodPost, headers: map[string]string{"Sec-Fetch-Site": sameOrigin, "Origin": "http://localhost:5173"}},
		{name: "no Sec-Fetch-Site, the orchestrator's own Origin", method: http.MethodPost, headers: map[string]string{"Origin": "http://localhost:8080"}},
		{name: "the browser says cross-site", method: http.MethodPost, headers: map[string]string{"Sec-Fetch-Site": crossSite}, refused: true},
		{name: "the browser says same-site, which is another origin", method: http.MethodPost, headers: map[string]string{"Sec-Fetch-Site": "same-site"}, refused: true},
		{name: "a Sec-Fetch-Site value nobody defined", method: http.MethodPost, headers: map[string]string{"Sec-Fetch-Site": "elsewhere"}, refused: true},
		{name: "cross-site is believed over a matching Origin", method: http.MethodPost, headers: map[string]string{"Sec-Fetch-Site": crossSite, "Origin": "http://localhost:8080"}, refused: true},
		{name: "no Sec-Fetch-Site, a foreign Origin", method: http.MethodPost, headers: map[string]string{"Origin": evilOrigin}, refused: true},
		{name: "no Sec-Fetch-Site, the same name on another port", method: http.MethodPost, headers: map[string]string{"Origin": "http://localhost:5173"}, refused: true},
		{name: "no Sec-Fetch-Site, Origin null", method: http.MethodPost, headers: map[string]string{"Origin": "null"}, refused: true},
		// What rides with a cross-site write excuses nothing: a script can
		// send JSON, and the browser attaches the site's cookies by itself.
		{name: "cross-site with a JSON content type", method: http.MethodPost, headers: map[string]string{"Sec-Fetch-Site": crossSite, "Content-Type": "application/json"}, refused: true},
		{name: "cross-site with a cookie", method: http.MethodPost, headers: map[string]string{"Sec-Fetch-Site": crossSite, "Cookie": sessionCookieName + "=anything"}, refused: true},
		{name: "no Sec-Fetch-Site, a foreign Origin with a JSON content type", method: http.MethodPost, headers: map[string]string{"Origin": evilOrigin, "Content-Type": "application/json"}, refused: true},
		{name: "no Sec-Fetch-Site, a foreign Origin with a cookie", method: http.MethodPost, headers: map[string]string{"Origin": evilOrigin, "Cookie": sessionCookieName + "=anything"}, refused: true},
		{name: "a cross-site DELETE", method: http.MethodDelete, headers: map[string]string{"Sec-Fetch-Site": crossSite}, refused: true},
		{name: "a cross-site PATCH", method: http.MethodPatch, headers: map[string]string{"Sec-Fetch-Site": crossSite}, refused: true},
		{name: "a cross-site PUT", method: http.MethodPut, headers: map[string]string{"Sec-Fetch-Site": crossSite}, refused: true},
		// Reads are not writes. A page on another site cannot read the
		// answer, and the Host check is what stops a rebound page.
		{name: "a cross-site GET", method: http.MethodGet, headers: map[string]string{"Sec-Fetch-Site": crossSite, "Origin": evilOrigin}},
		{name: "a cross-site HEAD", method: http.MethodHead, headers: map[string]string{"Sec-Fetch-Site": crossSite}},
		{name: "a cross-site OPTIONS", method: http.MethodOptions, headers: map[string]string{"Sec-Fetch-Site": crossSite}},
	}
	for mode, build := range originModes(t) {
		h := build()
		for _, tt := range tests {
			t.Run(mode+"/"+tt.name, func(t *testing.T) {
				rec := sendFrom(h, tt.method, "/api/v1/agents", host, tt.headers)
				got := refusalOf(rec)
				if !tt.refused {
					assert.NotEqual(t, crossOriginRefusalText, got.Error, "status=%d", rec.Code)
					return
				}
				require.Equal(t, http.StatusForbidden, rec.Code, "body=%s", rec.Body.String())
				assert.Equal(t, errorResponse{Code: "FORBIDDEN", Error: crossOriginRefusalText}, got)
			})
		}
	}
}

// TestOriginChecks_ASessionDoesNotExcuseACrossSiteWrite: with sign-in on, a
// write the browser marks as cross-site is refused whatever credential rides
// with it, and the answer is the cross-origin one, not "sign in first".
func TestOriginChecks_ASessionDoesNotExcuseACrossSiteWrite(t *testing.T) {
	srv, _ := newAuthServer(t, nil)
	h := withOriginChecks(srv)
	req := httptest.NewRequest(http.MethodPost, "/api/v1/auth/login",
		strings.NewReader(`{"username":"alice","password":"s3cret"}`))
	req.Host = "localhost:8080"
	req.Header.Set("Content-Type", "application/json")
	login := httptest.NewRecorder()
	h.ServeHTTP(login, req)
	require.Equal(t, http.StatusOK, login.Code, "body=%s", login.Body.String())
	token := decodeLogin(t, login).Token
	require.NotEmpty(t, token)

	// The cookie is the credential a browser attaches by itself, so it is
	// the one a page on another site would ride on.
	req = httptest.NewRequest(http.MethodPost, "/api/v1/auth/login",
		strings.NewReader(`{"username":"alice","password":"s3cret","session_transport":"cookie"}`))
	req.Host = "localhost:8080"
	req.Header.Set("Content-Type", "application/json")
	cookieLogin := httptest.NewRecorder()
	h.ServeHTTP(cookieLogin, req)
	require.Equal(t, http.StatusOK, cookieLogin.Code, "body=%s", cookieLogin.Body.String())
	require.Len(t, cookieLogin.Result().Cookies(), 1)
	session := cookieLogin.Result().Cookies()[0]
	cookie := session.Name + "=" + session.Value

	for name, headers := range map[string]map[string]string{
		"no credential":      {"Sec-Fetch-Site": crossSite},
		"a bearer token":     {"Sec-Fetch-Site": crossSite, "Authorization": "Bearer " + token},
		"the session cookie": {"Sec-Fetch-Site": crossSite, "Cookie": cookie},
		"the session cookie and a foreign Origin, no Sec-Fetch-Site": {"Origin": evilOrigin, "Cookie": cookie},
	} {
		t.Run(name, func(t *testing.T) {
			rec := sendFrom(h, http.MethodPost, "/api/v1/sessions", "localhost:8080", headers)
			require.Equal(t, http.StatusForbidden, rec.Code)
			assert.Equal(t, crossOriginRefusalText, refusalOf(rec).Error)
		})
	}

	// Control: the same token on a request no browser marked is let through
	// to the route (503: this server has no channel store behind sessions).
	rec := sendFrom(h, http.MethodPost, "/api/v1/sessions", "localhost:8080",
		map[string]string{"Authorization": "Bearer " + token})
	assert.Equal(t, http.StatusServiceUnavailable, rec.Code, "body=%s", rec.Body.String())
	// And the cookie on a write the browser marks same-origin, as the
	// console's own writes are.
	rec = sendFrom(h, http.MethodPost, "/api/v1/sessions", "localhost:8080",
		map[string]string{"Sec-Fetch-Site": sameOrigin, "Cookie": cookie})
	assert.Equal(t, http.StatusServiceUnavailable, rec.Code, "body=%s", rec.Body.String())
}

// formPost is what a browser sends when a page on another site submits a
// plain HTML form to the orchestrator: no script, no preflight.
var formPost = map[string]string{
	"Content-Type":   "application/x-www-form-urlencoded",
	"Origin":         evilOrigin,
	"Sec-Fetch-Site": crossSite,
	"Sec-Fetch-Mode": "navigate",
	"Sec-Fetch-Dest": "document",
}

// TestOriginChecks_BodylessRoutesRefuseACrossSiteFormPost covers the three
// routes that take no body and so never asked for a JSON content type: a
// page on any site could call them with a form. Each is refused and changes
// nothing; the same call from a client that is not a browser still works.
func TestOriginChecks_BodylessRoutesRefuseACrossSiteFormPost(t *testing.T) {
	const host = "localhost:8080"

	t.Run("session archive", func(t *testing.T) {
		srv, _ := sessionTestServer(t)
		created := createSessionViaREST(t, srv, "kept")
		h := withOriginChecks(srv)
		archived := func() bool {
			rec := sendFrom(h, http.MethodGet, "/api/v1/sessions/"+created.ID, host, nil)
			require.Equal(t, http.StatusOK, rec.Code)
			var got sessionResponse
			require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &got))
			return got.Archived
		}

		rec := sendFrom(h, http.MethodPost, "/api/v1/sessions/"+created.ID+"/archive", host, formPost)
		require.Equal(t, http.StatusForbidden, rec.Code, "body=%s", rec.Body.String())
		assert.Equal(t, crossOriginRefusalText, refusalOf(rec).Error)
		assert.False(t, archived(), "the refused post archived nothing")

		rec = sendFrom(h, http.MethodPost, "/api/v1/sessions/"+created.ID+"/archive", host, nil)
		require.Equal(t, http.StatusNoContent, rec.Code)
		assert.True(t, archived(), "control: the same call without the browser's marks archives it")
	})

	t.Run("unquarantine", func(t *testing.T) {
		cb := breakerWithThreshold(t)
		srv := testServerWithBreaker(t, cb)
		quarantineAgentForTest(t, cb, "agent-a")
		h := withOriginChecks(srv)

		rec := sendFrom(h, http.MethodPost, "/api/v1/agents/agent-a/unquarantine", host, formPost)
		require.Equal(t, http.StatusForbidden, rec.Code, "body=%s", rec.Body.String())
		assert.Equal(t, crossOriginRefusalText, refusalOf(rec).Error)
		assert.True(t, cb.IsQuarantined("agent-a"), "the refused post released nobody")

		rec = sendFrom(h, http.MethodPost, "/api/v1/agents/agent-a/unquarantine", host, nil)
		require.Equal(t, http.StatusNoContent, rec.Code, "body=%s", rec.Body.String())
		assert.False(t, cb.IsQuarantined("agent-a"), "control: the same call without the browser's marks releases the agent")
	})

	t.Run("convene on an armed channel", func(t *testing.T) {
		woken := &countingDispatcher{}
		srv, id := channelConfigTestServerWithDispatcher(t, true, []channels.Member{
			{ParticipantID: "alice", RespondPolicy: channels.RespondAlways},
			{ParticipantID: "bob", RespondPolicy: channels.RespondAlways},
		}, woken)
		armConvene(t, srv, id, "alice")
		h := withOriginChecks(srv)

		rec := sendFrom(h, http.MethodPost, "/api/v1/channels/"+id+"/convene", host, formPost)
		require.Equal(t, http.StatusForbidden, rec.Code, "body=%s", rec.Body.String())
		assert.Equal(t, crossOriginRefusalText, refusalOf(rec).Error)
		assert.Equal(t, int64(0), woken.calls.Load(), "the refused post woke no convener")

		rec = sendFrom(h, http.MethodPost, "/api/v1/channels/"+id+"/convene", host, nil)
		require.Equal(t, http.StatusAccepted, rec.Code, "body=%s", rec.Body.String())
		assert.Equal(t, int64(1), woken.calls.Load(), "control: the same call without the browser's marks wakes the convener")
	})
}

// countingDispatcher counts the events the channel router hands it, and
// delivers each.
type countingDispatcher struct{ calls atomic.Int64 }

func (d *countingDispatcher) Dispatch(context.Context, channels.DispatchEnvelope, channels.ChannelMessage) error {
	d.calls.Add(1)
	return nil
}

// TestOriginChecks_ARefusedWriteIsLogged: the operator who wonders why a
// write failed finds what the browser sent.
func TestOriginChecks_ARefusedWriteIsLogged(t *testing.T) {
	core, logs := observer.New(zapcore.WarnLevel)
	srv, _ := testServer(t)
	srv.logger = zap.New(core)

	rec := sendFrom(withOriginChecks(srv), http.MethodPost, "/api/v1/sessions/s/archive", "localhost:8080", formPost)

	require.Equal(t, http.StatusForbidden, rec.Code)
	require.Equal(t, 1, logs.Len())
	entry := logs.All()[0]
	assert.Equal(t, "request refused: a write that the browser marks as coming from another origin", entry.Message)
	fields := entry.ContextMap()
	assert.Equal(t, http.MethodPost, fields["method"])
	assert.Equal(t, "/api/v1/sessions/s/archive", fields["path"])
	assert.Equal(t, evilOrigin, fields["origin"])
	assert.Equal(t, crossSite, fields["sec_fetch_site"])

	// The client chose both values, so a long one is cut as a long Host is.
	logs.TakeAll()
	longOrigin := "http://" + strings.Repeat("a", 300) + ".example"
	longMark := strings.Repeat("x", 300)
	rec = sendFrom(withOriginChecks(srv), http.MethodPost, "/api/v1/sessions/s/archive", "localhost:8080",
		map[string]string{"Origin": longOrigin, "Sec-Fetch-Site": longMark})
	require.Equal(t, http.StatusForbidden, rec.Code)
	require.Equal(t, 1, logs.Len())
	fields = logs.All()[0].ContextMap()
	assert.Equal(t, longOrigin[:64]+"…", fields["origin"])
	assert.Equal(t, longMark[:64]+"…", fields["sec_fetch_site"])
}
