package server

import (
	"encoding/json"
	"net/http"
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/channels"
	"github.com/mkhomutov/persatrix/internal/planner"
	"github.com/mkhomutov/persatrix/internal/registry"
	"github.com/mkhomutov/persatrix/internal/state"
)

// channelTestServer wires a real on-disk SQLite channels store + router
// onto a fresh test Server. Uses t.TempDir so each test is isolated.
func channelTestServer(t *testing.T) (*Server, channels.ChannelStore) {
	t.Helper()
	dbPath := filepath.Join(t.TempDir(), "channels.db")
	store, err := channels.NewSQLiteStore(dbPath, channels.SQLiteOptions{
		MaxChannels: 50,
		Logger:      zap.NewNop(),
	})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })
	router := channels.NewChannelRouter(store, channels.NoopDispatcher{}, zap.NewNop(), nil)

	wfDir := t.TempDir()
	logger := zap.NewNop()
	srv, err := New("127.0.0.1:0", wfDir,
		state.NewInMemoryStore(logger),
		registry.NewInMemoryRegistry(logger),
		planner.NewYAMLPlanner(logger),
		logger,
		WithChannels(store, router),
	)
	require.NoError(t, err)
	return srv, store
}

// TestChannels_CreateChannel_Created pins the happy path: 201 with the
// canonical id derived server-side.
func TestChannels_CreateChannel_Created(t *testing.T) {
	srv, _ := channelTestServer(t)
	body, _ := json.Marshal(createChannelRequest{
		Name:        "planning",
		Description: "Sprint planning",
		Members: []channelMemberRequest{
			{ID: "alice", Respond: "always"},
			{ID: "bob"},
		},
	})
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels", body)
	require.Equal(t, http.StatusCreated, rec.Code, "body=%s", rec.Body.String())

	var resp channelResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	assert.Equal(t, "group:planning", resp.ID)
	assert.Equal(t, "planning", resp.Name)
	assert.Equal(t, "group", resp.Type)
}

// TestChannels_CreateChannel_RejectsEmptyName pins boundary validation.
func TestChannels_CreateChannel_RejectsEmptyName(t *testing.T) {
	srv, _ := channelTestServer(t)
	body, _ := json.Marshal(createChannelRequest{Members: []channelMemberRequest{{ID: "alice"}}})
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels", body)
	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

// TestChannels_CreateChannel_Conflict pins ErrChannelExists → 409.
func TestChannels_CreateChannel_Conflict(t *testing.T) {
	srv, _ := channelTestServer(t)
	body, _ := json.Marshal(createChannelRequest{
		Name:    "planning",
		Members: []channelMemberRequest{{ID: "alice"}},
	})
	require.Equal(t, http.StatusCreated,
		doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels", body).Code)
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels", body)
	assert.Equal(t, http.StatusConflict, rec.Code)
}

// TestChannels_GetChannel_NotFound pins ErrChannelNotFound → 404.
func TestChannels_GetChannel_NotFound(t *testing.T) {
	srv, _ := channelTestServer(t)
	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/channels/group:nope", nil)
	assert.Equal(t, http.StatusNotFound, rec.Code)
}

// TestChannels_PublishMessage_HappyPath_PersistsAndReturnsCreated pins the
// publish-then-history round-trip.
func TestChannels_PublishMessage_HappyPath_PersistsAndReturnsCreated(t *testing.T) {
	srv, _ := channelTestServer(t)
	createBody, _ := json.Marshal(createChannelRequest{
		Name:    "planning",
		Members: []channelMemberRequest{{ID: "alice"}, {ID: "bob"}},
	})
	require.Equal(t, http.StatusCreated,
		doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels", createBody).Code)

	pubBody, _ := json.Marshal(publishMessageRequest{SenderID: "alice", Content: "hello"})
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels/group:planning/messages", pubBody)
	require.Equal(t, http.StatusCreated, rec.Code, "body=%s", rec.Body.String())

	var msg channelMessageResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &msg))
	assert.Equal(t, "group:planning", msg.ChannelID)
	assert.Equal(t, "alice", msg.SenderID)
	assert.NotEmpty(t, msg.ID)

	// History reflects the publish.
	histRec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/channels/group:planning/messages", nil)
	require.Equal(t, http.StatusOK, histRec.Code)
	var hist historyResponse
	require.NoError(t, json.Unmarshal(histRec.Body.Bytes(), &hist))
	require.Len(t, hist.Messages, 1)
	assert.Equal(t, "hello", hist.Messages[0].Content)
}

// TestChannels_PublishMessage_NonMember_Forbidden pins ErrNotMember → 403.
func TestChannels_PublishMessage_NonMember_Forbidden(t *testing.T) {
	srv, _ := channelTestServer(t)
	createBody, _ := json.Marshal(createChannelRequest{
		Name:    "planning",
		Members: []channelMemberRequest{{ID: "alice"}},
	})
	require.Equal(t, http.StatusCreated,
		doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels", createBody).Code)

	pubBody, _ := json.Marshal(publishMessageRequest{SenderID: "intruder", Content: "x"})
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels/group:planning/messages", pubBody)
	assert.Equal(t, http.StatusForbidden, rec.Code)
}

// TestChannels_PublishMessage_ChannelTypeMismatch_BadRequest pins the
// RFC 0011 §C `channel_type` cross-validation.
func TestChannels_PublishMessage_ChannelTypeMismatch_BadRequest(t *testing.T) {
	srv, _ := channelTestServer(t)
	createBody, _ := json.Marshal(createChannelRequest{
		Name:    "planning",
		Members: []channelMemberRequest{{ID: "alice"}},
	})
	require.Equal(t, http.StatusCreated,
		doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels", createBody).Code)

	pubBody, _ := json.Marshal(publishMessageRequest{
		SenderID: "alice", Content: "x", ChannelType: "dm",
	})
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels/group:planning/messages", pubBody)
	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

// TestChannels_PublishMessage_MissingFields pins SenderID + Content checks.
func TestChannels_PublishMessage_MissingFields(t *testing.T) {
	srv, _ := channelTestServer(t)
	createBody, _ := json.Marshal(createChannelRequest{
		Name:    "planning",
		Members: []channelMemberRequest{{ID: "alice"}},
	})
	doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels", createBody)

	cases := []struct {
		name string
		body publishMessageRequest
	}{
		{"missing sender", publishMessageRequest{Content: "x"}},
		{"missing content", publishMessageRequest{SenderID: "alice"}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			b, _ := json.Marshal(c.body)
			rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels/group:planning/messages", b)
			assert.Equal(t, http.StatusBadRequest, rec.Code)
		})
	}
}

// TestChannels_GetThread_ReturnsReplies pins the thread-fetch endpoint.
func TestChannels_GetThread_ReturnsReplies(t *testing.T) {
	srv, _ := channelTestServer(t)
	createBody, _ := json.Marshal(createChannelRequest{
		Name:    "planning",
		Members: []channelMemberRequest{{ID: "alice"}, {ID: "bob"}},
	})
	require.Equal(t, http.StatusCreated,
		doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels", createBody).Code)

	parentBody, _ := json.Marshal(publishMessageRequest{SenderID: "alice", Content: "parent"})
	parentRec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels/group:planning/messages", parentBody)
	require.Equal(t, http.StatusCreated, parentRec.Code)
	var parent channelMessageResponse
	require.NoError(t, json.Unmarshal(parentRec.Body.Bytes(), &parent))

	replyBody, _ := json.Marshal(publishMessageRequest{
		SenderID: "bob", Content: "reply", ThreadID: parent.ID,
	})
	require.Equal(t, http.StatusCreated,
		doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels/group:planning/messages", replyBody).Code)

	rec := doRequest(srv.Handler(), http.MethodGet,
		"/api/v1/channels/group:planning/messages/"+parent.ID+"/thread", nil)
	require.Equal(t, http.StatusOK, rec.Code, "body=%s", rec.Body.String())
	var hist historyResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &hist))
	require.Len(t, hist.Messages, 1)
	assert.Equal(t, "reply", hist.Messages[0].Content)
}

// TestChannels_AddMember_NoContent pins the add-member endpoint.
func TestChannels_AddMember_NoContent(t *testing.T) {
	srv, store := channelTestServer(t)
	createBody, _ := json.Marshal(createChannelRequest{
		Name:    "planning",
		Members: []channelMemberRequest{{ID: "alice"}},
	})
	require.Equal(t, http.StatusCreated,
		doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels", createBody).Code)

	addBody, _ := json.Marshal(addMemberRequest{ID: "carol", Respond: "always"})
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels/group:planning/members", addBody)
	assert.Equal(t, http.StatusNoContent, rec.Code)

	members, err := store.GetMembers(t.Context(), "group:planning")
	require.NoError(t, err)
	require.Len(t, members, 2)
}

// TestChannels_ListChannels pins the listing endpoint and limit cap.
func TestChannels_ListChannels(t *testing.T) {
	srv, _ := channelTestServer(t)
	for _, n := range []string{"alpha", "beta", "gamma"} {
		body, _ := json.Marshal(createChannelRequest{
			Name:    n,
			Members: []channelMemberRequest{{ID: "alice"}},
		})
		require.Equal(t, http.StatusCreated,
			doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels", body).Code)
	}
	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/channels?limit=2", nil)
	require.Equal(t, http.StatusOK, rec.Code)
	var resp listChannelsResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	assert.Len(t, resp.Channels, 2)
}

// TestChannels_Endpoints_503WhenStoreUnset pins the nil-safe degradation
// path when the orchestrator is started without a channel store.
func TestChannels_Endpoints_503WhenStoreUnset(t *testing.T) {
	srv, _ := testServer(t) // no WithChannels
	for _, path := range []string{
		"/api/v1/channels",
		"/api/v1/channels/group:x",
		"/api/v1/channels/group:x/messages",
	} {
		rec := doRequest(srv.Handler(), http.MethodGet, path, nil)
		assert.Equal(t, http.StatusServiceUnavailable, rec.Code, "path=%s", path)
	}
}
