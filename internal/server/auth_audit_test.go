package server

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/mkhomutov/persatrix/internal/accounts"
	"github.com/mkhomutov/persatrix/internal/security"
)

// RFC 0039 PR 4 (Phase 1 step 10) — `auth.*` audit emission. The events
// record METADATA ONLY: username, source, role, transport — never the
// password and never the raw token (§Security "Audit").

func eventsOfType(evs []security.AuditEvent, t security.AuditEventType) []security.AuditEvent {
	var out []security.AuditEvent
	for _, ev := range evs {
		if ev.EventType == t {
			out = append(out, ev)
		}
	}
	return out
}

func TestLoginEmitsSucceededAndFailedAuditEvents(t *testing.T) {
	auditor := &recordingAuditor{}
	srv, _ := newAuthServer(t, nil, WithAuditLogger(auditor))
	h := srv.Handler()

	require.Equal(t, http.StatusOK,
		postLogin(t, h, `{"username":"Alice","password":"s3cret"}`, nil).Code)
	require.Equal(t, http.StatusUnauthorized,
		postLogin(t, h, `{"username":"alice","password":"wrong"}`, nil).Code)

	succeeded := eventsOfType(auditor.all(), security.AuditAuthLoginSucceeded)
	require.Len(t, succeeded, 1)
	ev := succeeded[0]
	assert.Equal(t, "alice", ev.Detail["username"], "the folded username, as stored")
	assert.Equal(t, accounts.RoleOperator, ev.Detail["role"])
	assert.Equal(t, "bearer", ev.Detail["transport"])
	assert.NotEmpty(t, ev.Detail["source"], "the §B limiter's source key doubles as the audit source")

	failed := eventsOfType(auditor.all(), security.AuditAuthLoginFailed)
	require.Len(t, failed, 1)
	assert.Equal(t, "alice", failed[0].Detail["username"], "the attempted username — never the attempted password")
	assert.Equal(t, "invalid_credentials", failed[0].Detail["reason"])
}

func TestLoginFailedAuditDistinguishesDisabledAccountInternally(t *testing.T) {
	// The WIRE response for a disabled account is the identical 401
	// (§C non-disclosure) — but the operator-side audit record keeps
	// the true reason: a disabled account being probed is a signal.
	auditor := &recordingAuditor{}
	srv, store := newAuthServer(t, nil, WithAuditLogger(auditor))
	h := srv.Handler()

	acct, err := store.GetAccountByUsername(context.Background(), "alice")
	require.NoError(t, err)
	require.NoError(t, store.SetAccountStatus(context.Background(), acct.ID, accounts.StatusDisabled))

	rec := postLogin(t, h, `{"username":"alice","password":"s3cret"}`, nil)
	require.Equal(t, http.StatusUnauthorized, rec.Code)

	failed := eventsOfType(auditor.all(), security.AuditAuthLoginFailed)
	require.Len(t, failed, 1)
	assert.Equal(t, "account_disabled", failed[0].Detail["reason"])
}

func TestLogoutEmitsAuditEvent(t *testing.T) {
	auditor := &recordingAuditor{}
	srv, _ := newAuthServer(t, nil, WithAuditLogger(auditor))
	h := srv.Handler()

	rec := postLogin(t, h, `{"username":"alice","password":"s3cret"}`, nil)
	require.Equal(t, http.StatusOK, rec.Code)
	token := decodeLogin(t, rec).Token

	req := httptest.NewRequest(http.MethodPost, "/api/v1/auth/logout", nil)
	req.Header.Set("Authorization", "Bearer "+token)
	out := httptest.NewRecorder()
	h.ServeHTTP(out, req)
	require.Equal(t, http.StatusNoContent, out.Code)

	logouts := eventsOfType(auditor.all(), security.AuditAuthLogout)
	require.Len(t, logouts, 1)
	ev := logouts[0]
	assert.Equal(t, "alice", ev.Detail["username"], "the revoked session's account is resolved before revocation")
	assert.Equal(t, "bearer", ev.Detail["transport"])

	// A logout presenting an unknown token revokes nothing and audits
	// nothing (the RFC names auth.logout for actual logouts only).
	req = httptest.NewRequest(http.MethodPost, "/api/v1/auth/logout", nil)
	req.Header.Set("Authorization", "Bearer not-a-real-token")
	out = httptest.NewRecorder()
	h.ServeHTTP(out, req)
	require.Equal(t, http.StatusUnauthorized, out.Code)
	assert.Len(t, eventsOfType(auditor.all(), security.AuditAuthLogout), 1)
}

func TestAuthAuditNeverRecordsPasswordOrToken(t *testing.T) {
	auditor := &recordingAuditor{}
	srv, _ := newAuthServer(t, nil, WithAuditLogger(auditor))
	h := srv.Handler()

	rec := postLogin(t, h, `{"username":"alice","password":"s3cret"}`, nil)
	require.Equal(t, http.StatusOK, rec.Code)
	token := decodeLogin(t, rec).Token
	require.NotEmpty(t, token)
	postLogin(t, h, `{"username":"alice","password":"wrong-guess"}`, nil)

	req := httptest.NewRequest(http.MethodPost, "/api/v1/auth/logout", nil)
	req.Header.Set("Authorization", "Bearer "+token)
	out := httptest.NewRecorder()
	h.ServeHTTP(out, req)
	require.Equal(t, http.StatusNoContent, out.Code)

	blob, err := json.Marshal(auditor.all())
	require.NoError(t, err)
	assert.NotContains(t, string(blob), "s3cret")
	assert.NotContains(t, string(blob), "wrong-guess")
	assert.NotContains(t, string(blob), token, "the raw session token never enters the audit sink")
}

func TestAuthAuditEventsAreSecurityClass(t *testing.T) {
	// Auth lifecycle events mirror agent.token_issued / token_invalid:
	// per-event fsync, because losing them on crash defeats the audit —
	// and their rate is human-scale, bounded by the §B limiters.
	for _, et := range []security.AuditEventType{
		security.AuditAuthLoginSucceeded,
		security.AuditAuthLoginFailed,
		security.AuditAuthLogout,
	} {
		assert.True(t, security.IsSecurityEvent(et), "%s must be security-class", et)
	}
}
