package server

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// RFC 0039 PR 5 — the §F verified `participant_id` claim and the §H
// unquarantine env-token supersession.

func TestChatUsesVerifiedClaimNotBodyUserID(t *testing.T) {
	h5 := newEnforcedServer(t, nil)
	registerHealthyAgent(t, h5.registry, "ember-owl", "Ember Owl")
	h := h5.srv.Handler()
	bob := bearerFor(t, h, "bob")

	// The simulated agent reply is published on BOB-PARTICIPANT's DM: if
	// the handler had honoured the spoofed body user_id, the await would
	// sit on mallory's DM and never see this message.
	publishReplyAfter(t, h5.router, h5.chStore, "bob-participant", "ember-owl",
		"hi bob", 20*time.Millisecond)

	body, _ := json.Marshal(chatRequest{Message: "hello", UserID: "mallory"})
	req := httptest.NewRequest(http.MethodPost, "/api/v1/agents/ember-owl/chat", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+bob)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	require.Equal(t, http.StatusOK, rec.Code, rec.Body.String())

	// §F: the spoofed id materialised NOTHING — no DM exists for it.
	_, err := h5.chStore.LookupDM(context.Background(), "mallory", "ember-owl")
	assert.Error(t, err, "the body user_id must be ignored under enabled — no DM for the spoofed id")
	_, err = h5.chStore.LookupDM(context.Background(), "bob-participant", "ember-owl")
	assert.NoError(t, err, "the verified claim's DM is the one that exists")
}

func TestChatHistoryHonoursClaim(t *testing.T) {
	h5 := newEnforcedServer(t, nil)
	h := h5.srv.Handler()
	bob := bearerFor(t, h, "bob")

	// Absent user_id defaults to the claim: 200 empty history (the
	// fresh-start contract), NOT the disabled-mode 400.
	rec := request(h, http.MethodGet, "/api/v1/agents/ember-owl/chat/history", bob)
	assert.Equal(t, http.StatusOK, rec.Code, rec.Body.String())

	// A user_id naming the claim is accepted verbatim.
	rec = request(h, http.MethodGet, "/api/v1/agents/ember-owl/chat/history?user_id=bob-participant", bob)
	assert.Equal(t, http.StatusOK, rec.Code)

	// A user_id naming someone ELSE is refused loudly — the coarse gate
	// has no cross-user read story until Phase 3+ (the v0.2 TODO the §F
	// claim closes).
	rec = request(h, http.MethodGet, "/api/v1/agents/ember-owl/chat/history?user_id=mallory", bob)
	assert.Equal(t, http.StatusForbidden, rec.Code)
}

func TestUnquarantineEnvTokenSupersededUnderEnabled(t *testing.T) {
	// §H: under `enabled` the role gate is the control and
	// SECURITY_UNQUARANTINE_TOKEN is IGNORED — the Authorization header
	// carries a session token now, which could never equal the shared
	// secret. Under `disabled` the env-token check still applies
	// (pinned by the PR #244 tests in server_unquarantine_test.go,
	// which run with no auth subsystem wired).
	h5 := newEnforcedServer(t, nil)
	WithRateLimiter(nil, breakerWithThreshold(t))(h5.srv)
	WithUnquarantineToken("shared-secret")(h5.srv)
	h := h5.srv.Handler()

	// Operator session, no shared secret presented: the request passes
	// the (ignored) token gate and reaches the breaker — 404
	// not-quarantined, never the token 401.
	op := bearerFor(t, h, "alice")
	rec := request(h, http.MethodPost, "/api/v1/agents/clean-agent/unquarantine", op)
	assert.Equal(t, http.StatusNotFound, rec.Code, rec.Body.String())

	// The §E gate still holds around it: user → 403, anonymous → 401.
	bob := bearerFor(t, h, "bob")
	assert.Equal(t, http.StatusForbidden,
		request(h, http.MethodPost, "/api/v1/agents/clean-agent/unquarantine", bob).Code)
	assert.Equal(t, http.StatusUnauthorized,
		request(h, http.MethodPost, "/api/v1/agents/clean-agent/unquarantine", "").Code)
}
