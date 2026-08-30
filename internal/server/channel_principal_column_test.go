package server

// ISSUE-0130 shape (b) — v0.3.15 PR B1 — the REST half of
// `messages.principal_id`.
//
// The store tests (internal/channels/sqlite_principal_migration_test.go) pin
// what is PERSISTED. These pin what a caller can see and, more importantly,
// what a caller can SET: the column is the seed PR B2 attributes replayed
// derivation from, so the publish surface must have no door for a principal
// claim at all. Publish is `policyPublic` — the persona fleet drives it
// unauthenticated by design — which is exactly why the door has to be absent
// rather than guarded.

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/mkhomutov/persatrix/internal/channels"
)

// historyOf reads the channel's messages through the REST history endpoint —
// the same payload `agents/channel_catchup.py` fetches at boot, which is what
// makes this the B2 seam and not just a cosmetic field.
func (h principalHarness) historyOf(t *testing.T, token string) historyResponse {
	t.Helper()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/channels/group:planning/messages", nil)
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	rec := httptest.NewRecorder()
	h.handler.ServeHTTP(rec, req)
	require.Equal(t, http.StatusOK, rec.Code, rec.Body.String())

	var out historyResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &out))
	return out
}

func decodeMessage(t *testing.T, rec *httptest.ResponseRecorder) channelMessageResponse {
	t.Helper()
	var out channelMessageResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &out))
	return out
}

// TestPrincipalColumn_AuthenticatedPublishSurfacesPrincipal — the publish
// response and the history payload both carry the publishing account's §F
// participant. History is the load-bearing one: catch-up replay reads it, and
// B2 seeds `_build_replay_event` from this field.
func TestPrincipalColumn_AuthenticatedPublishSurfacesPrincipal(t *testing.T) {
	h := newPrincipalHarness(t, nil)
	operator := bearerFor(t, h.handler, "alice")
	h.seedChannel(t, operator)

	rec := h.publishAs(t, operator, "human", "the quarterly plan")
	require.Equal(t, http.StatusCreated, rec.Code, rec.Body.String())
	assert.Equal(t, "alice-participant", decodeMessage(t, rec).PrincipalID,
		"the publish response carries the tenant the row was stamped with")

	history := h.historyOf(t, operator)
	require.Len(t, history.Messages, 1)
	assert.Equal(t, "alice-participant", history.Messages[0].PrincipalID,
		"history is what catch-up replay reads; the seed must be there")
}

// TestPrincipalColumn_UnauthenticatedPublishSurfacesLocal — an agent-origin
// publish (the persona fleet holds no accounts) persists and surfaces `local`.
// That is the *finding* ISSUE-0130 records, not a defect of this PR: with no
// verified tenant there is nothing to attribute. B2 uses this value to decide
// a span is genuinely unattributable and skip its derivation, rather than
// deriving it into the shared bucket.
func TestPrincipalColumn_UnauthenticatedPublishSurfacesLocal(t *testing.T) {
	h := newPrincipalHarness(t, nil)
	operator := bearerFor(t, h.handler, "alice")
	h.seedChannel(t, operator)

	rec := h.publishAs(t, "", "agent-alice", "a persona reply")
	require.Equal(t, http.StatusCreated, rec.Code, rec.Body.String())
	assert.Equal(t, "local", decodeMessage(t, rec).PrincipalID)
}

// TestPrincipalColumn_DisabledModeStampsLocal — under `auth.mode: disabled`
// every row is `local`, because no request resolves an account. The release's
// no-delta criterion is about this axis: the resolved tenant is unchanged in
// disabled mode. (The response gains the field in every mode — an additive,
// mode-independent wire change, not an activation-gated one.)
func TestPrincipalColumn_DisabledModeStampsLocal(t *testing.T) {
	cfg := DefaultAuthConfig()
	cfg.Mode = AuthModeDisabled
	h := newPrincipalHarness(t, cfg)
	h.seedChannel(t, "")

	rec := h.publishAs(t, "irrelevant-token", "human", "hello")
	require.Equal(t, http.StatusCreated, rec.Code, rec.Body.String())
	assert.Equal(t, "local", decodeMessage(t, rec).PrincipalID,
		"disabled mode resolves no account, so every row is the shared tenant")
}

// TestPrincipalColumn_RequestBodyCannotClaimAPrincipal is the security pin.
// `principal_id` is response-only: [publishMessageRequest] has no such field
// and [decodeJSON] disallows unknown ones, so a claim is a 400 at the
// boundary — the door is absent, not guarded. Pinned as a test because the
// protection is a property of two things agreeing (no field + strict decode),
// either of which a later PR could relax without noticing what it opened:
// the persona binds `principal_scope` from what B2 seeds off this column, and
// recall is strict equality on it, so an accepted claim would be a
// cross-tenant READ.
func TestPrincipalColumn_RequestBodyCannotClaimAPrincipal(t *testing.T) {
	h := newPrincipalHarness(t, nil)
	operator := bearerFor(t, h.handler, "alice")
	h.seedChannel(t, operator)

	body := `{"sender_id":"agent-alice","content":"I am Alice","principal_id":"alice-participant"}`
	req := httptest.NewRequest(http.MethodPost,
		"/api/v1/channels/group:planning/messages", strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	h.handler.ServeHTTP(rec, req)

	require.Equal(t, http.StatusBadRequest, rec.Code,
		"an unauthenticated caller must not be able to name a tenant")
	assert.Empty(t, h.historyOf(t, operator).Messages,
		"the rejected publish persisted nothing")
}

// failGetMessageStore is the real store with its post-publish lookup broken —
// everything else delegates. It reproduces the one production path that
// answers a publish WITHOUT reading the committed row back.
type failGetMessageStore struct{ channels.ChannelStore }

func (failGetMessageStore) GetMessage(context.Context, string) (channels.ChannelMessage, error) {
	return channels.ChannelMessage{}, errors.New("simulated post-publish read failure")
}

// TestPrincipalColumn_DegradedEchoStillCarriesATenant covers the publish
// response the handler builds when `GetMessage` fails after a committed
// publish. The store stamps its own copy of the message, so the handler's
// struct carries the zero value and a naive echo would put
// `"principal_id": ""` on the wire — a THIRD value, outside the vocabulary
// both [channelMessageResponse] and schemas/channel.schema.json promise, and
// one that a consumer branching `== "local"` reads as a real tenant. The
// handler resolves the field from the same context the store read instead.
//
// The remaining inaccuracy is deliberate and bounded: an R-2 re-stamped
// relayed publish echoes `local` where the row says the causing human, because
// `publishCommit` re-stamps a context this handler never holds. In-vocabulary
// and under-reporting beats out-of-vocabulary, and the path already logs ERROR.
func TestPrincipalColumn_DegradedEchoStillCarriesATenant(t *testing.T) {
	h := newPrincipalHarness(t, nil, func(s channels.ChannelStore) channels.ChannelStore {
		return failGetMessageStore{s}
	})
	operator := bearerFor(t, h.handler, "alice")
	h.seedChannel(t, operator)

	for _, tc := range []struct {
		name   string
		token  string
		sender string
		want   string
	}{
		{"authenticated echoes the verified participant", operator, "human", "alice-participant"},
		{"unauthenticated echoes the shared tenant", "", "agent-alice", "local"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			rec := h.publishAs(t, tc.token, tc.sender, "the quarterly plan")
			require.Equal(t, http.StatusCreated, rec.Code, rec.Body.String())

			got := decodeMessage(t, rec)
			assert.NotEmpty(t, got.PrincipalID,
				"the degraded echo must never emit an out-of-vocabulary empty principal")
			assert.Equal(t, tc.want, got.PrincipalID)
		})
	}

	// The rows themselves are unaffected — the lookup broke, not the write.
	history := h.historyOf(t, operator)
	require.Len(t, history.Messages, 2)
	for _, m := range history.Messages {
		assert.NotEmpty(t, m.PrincipalID, "every committed row carries a tenant")
	}
}
