package server

import (
	"context"
	"encoding/json"
	"net/http"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/mkhomutov/persatrix/internal/channels"
)

// RFC 0048 amendment §B — GET /api/v1/agents/{id}/chat/history resumes a
// conversation read-only so a console reload doesn't present as stateless.

// seedDM publishes `content` from `sender` into the (user,agent) DM, creating
// the DM if needed, so a history fetch has something to return.
func seedDM(t *testing.T, store channels.ChannelStore, router *channels.ChannelRouter, userID, agentID, sender, content string, ts time.Time) {
	t.Helper()
	dm, err := store.GetOrCreateDM(context.Background(), userID, agentID)
	require.NoError(t, err)
	require.NoError(t, router.Publish(context.Background(), channels.ChannelMessage{
		ID:        uuid.NewString(),
		ChannelID: dm.ID,
		SenderID:  sender,
		Content:   content,
		Timestamp: ts,
	}, ""))
}

// A persona never chatted with is the expected fresh-start case: 200 with an
// empty messages array, NOT 404 (Decision #2). Crucially the lookup is
// read-only — the fetch must not materialise a DM.
func TestHandleGetChatHistory_FreshStartReturnsEmpty(t *testing.T) {
	srv, reg, _, store := chatTestServer(t)
	registerHealthyAgent(t, reg, "ember-owl", "Ember Owl")

	rec := doRequest(srv.Handler(), http.MethodGet,
		"/api/v1/agents/ember-owl/chat/history?user_id=alice", nil)
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())

	var resp historyResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	assert.Empty(t, resp.Messages)
	assert.Contains(t, rec.Body.String(), `"messages":[]`)

	// Read-only: the never-used DM must not have been created by the fetch.
	_, err := store.LookupDM(context.Background(), "alice", "ember-owl")
	assert.ErrorIs(t, err, channels.ErrChannelNotFound)
}

// A used persona returns its persisted history newest-first, in the same shape
// as the channel-history endpoint, so the client reuses its parsing.
func TestHandleGetChatHistory_ReturnsHistoryNewestFirst(t *testing.T) {
	srv, reg, router, store := chatTestServer(t)
	registerHealthyAgent(t, reg, "ember-owl", "Ember Owl")

	base := time.Date(2026, 6, 2, 10, 0, 0, 0, time.UTC)
	seedDM(t, store, router, "alice", "ember-owl", "alice", "first", base)
	seedDM(t, store, router, "alice", "ember-owl", "ember-owl", "second", base.Add(time.Second))

	rec := doRequest(srv.Handler(), http.MethodGet,
		"/api/v1/agents/ember-owl/chat/history?user_id=alice", nil)
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())

	var resp historyResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	require.Len(t, resp.Messages, 2)
	assert.Equal(t, "second", resp.Messages[0].Content, "newest-first")
	assert.Equal(t, "first", resp.Messages[1].Content)
}

// History is scoped per (user, agent): a different principal's DM is invisible,
// which is the persistence/isolation story the endpoint exists to make visible.
func TestHandleGetChatHistory_ScopedToUser(t *testing.T) {
	srv, reg, router, store := chatTestServer(t)
	registerHealthyAgent(t, reg, "ember-owl", "Ember Owl")

	base := time.Date(2026, 6, 2, 10, 0, 0, 0, time.UTC)
	seedDM(t, store, router, "alice", "ember-owl", "alice", "alice's secret", base)

	// Bob has never chatted with this persona → empty, not alice's history.
	rec := doRequest(srv.Handler(), http.MethodGet,
		"/api/v1/agents/ember-owl/chat/history?user_id=bob", nil)
	require.Equal(t, http.StatusOK, rec.Code)

	var resp historyResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	assert.Empty(t, resp.Messages)
}

func TestHandleGetChatHistory_MissingUserIDIs400(t *testing.T) {
	srv, reg, _, _ := chatTestServer(t)
	registerHealthyAgent(t, reg, "ember-owl", "Ember Owl")

	rec := doRequest(srv.Handler(), http.MethodGet,
		"/api/v1/agents/ember-owl/chat/history", nil)
	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

// user_id == agent_id is not a DM — the canonical-id derivation rejects it,
// surfaced as a 400 (the same validation the chat POST path applies).
func TestHandleGetChatHistory_SelfDMIs400(t *testing.T) {
	srv, reg, _, _ := chatTestServer(t)
	registerHealthyAgent(t, reg, "ember-owl", "Ember Owl")

	rec := doRequest(srv.Handler(), http.MethodGet,
		"/api/v1/agents/ember-owl/chat/history?user_id=ember-owl", nil)
	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

// A malformed limit errors loudly, reusing the channel-history validation.
func TestHandleGetChatHistory_BadLimitIs400(t *testing.T) {
	srv, reg, _, _ := chatTestServer(t)
	registerHealthyAgent(t, reg, "ember-owl", "Ember Owl")

	rec := doRequest(srv.Handler(), http.MethodGet,
		"/api/v1/agents/ember-owl/chat/history?user_id=alice&limit=nope", nil)
	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

// The `before` keyset cursor is plumbed straight through to GetHistory (strict
// `timestamp < before`), so the endpoint paginates exactly like the
// channel-history surface it mirrors. The §F back-fill relies on this, so guard
// it: a `before` at the newer message's timestamp returns only the older one.
func TestHandleGetChatHistory_BeforePaginates(t *testing.T) {
	srv, reg, router, store := chatTestServer(t)
	registerHealthyAgent(t, reg, "ember-owl", "Ember Owl")

	base := time.Date(2026, 6, 2, 10, 0, 0, 0, time.UTC)
	seedDM(t, store, router, "alice", "ember-owl", "alice", "first", base)
	seedDM(t, store, router, "alice", "ember-owl", "ember-owl", "second", base.Add(time.Second))

	rec := doRequest(srv.Handler(), http.MethodGet,
		"/api/v1/agents/ember-owl/chat/history?user_id=alice&before=2026-06-02T10:00:01Z", nil)
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())

	var resp historyResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	require.Len(t, resp.Messages, 1, "only the message strictly before the cursor")
	assert.Equal(t, "first", resp.Messages[0].Content)
}

// A malformed `before` cursor errors loudly (RFC 3339 only), reusing the
// channel-history validation rather than silently ignoring the bad value.
func TestHandleGetChatHistory_BadBeforeIs400(t *testing.T) {
	srv, reg, _, _ := chatTestServer(t)
	registerHealthyAgent(t, reg, "ember-owl", "Ember Owl")

	rec := doRequest(srv.Handler(), http.MethodGet,
		"/api/v1/agents/ember-owl/chat/history?user_id=alice&before=nope", nil)
	assert.Equal(t, http.StatusBadRequest, rec.Code)
}
