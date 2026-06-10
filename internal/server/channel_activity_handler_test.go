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

// activityTestServer mirrors channelTestServer but hands back the router too, so
// the test can drain the detached fanout (which marks the in-flight set) before
// reading the activity endpoint — the REST publish returns at persistence, ahead
// of the goroutine that dispatches and marks.
func activityTestServer(t *testing.T) (*Server, channels.ChannelStore, *channels.ChannelRouter) {
	t.Helper()
	dbPath := filepath.Join(t.TempDir(), "channels.db")
	store, err := channels.NewSQLiteStore(dbPath, channels.SQLiteOptions{MaxChannels: 50, Logger: zap.NewNop()})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })
	router := channels.NewChannelRouter(store, channels.NoopDispatcher{}, zap.NewNop(), nil)

	logger := zap.NewNop()
	srv, err := New("127.0.0.1:0", t.TempDir(),
		state.NewInMemoryStore(logger),
		registry.NewInMemoryRegistry(logger),
		planner.NewYAMLPlanner(logger),
		logger,
		WithChannels(store, router),
	)
	require.NoError(t, err)
	return srv, store, router
}

func createActivityChannel(t *testing.T, srv *Server, router *channels.ChannelRouter) {
	t.Helper()
	body, _ := json.Marshal(createChannelRequest{
		Name: "planning",
		Members: []channelMemberRequest{
			{ID: "alice", Respond: "never"},
			{ID: "ember-owl", Respond: "always"},
			{ID: "crimson-fox", Respond: "always"},
		},
	})
	require.Equal(t, http.StatusCreated,
		doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels", body).Code)
	// Runtime groups default to floor control ON (applyRuntimeGroupGovernance),
	// which serializes the round so WaitForPendingFanout would block on each
	// speaker's full turn-timeout. Activity marking happens in fanout UPSTREAM of
	// the floor/concurrent split, so it is floor-independent — disable floor here
	// for a fast, deterministic concurrent fanout that drains promptly.
	router.SetFloorControl("group:planning", false, 0)
}

func getActivity(t *testing.T, srv *Server, channelID string) (int, channelActivityResponse) {
	t.Helper()
	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/channels/"+channelID+"/activity", nil)
	var resp channelActivityResponse
	if rec.Body.Len() > 0 {
		require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	}
	return rec.Code, resp
}

func TestChannelActivityEndpoint_ReportsThinkingResponders(t *testing.T) {
	srv, _, router := activityTestServer(t)
	createActivityChannel(t, srv, router)

	pub, _ := json.Marshal(publishMessageRequest{SenderID: "alice", Content: "standup time"})
	require.Equal(t, http.StatusCreated,
		doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels/group:planning/messages", pub).Code)
	router.WaitForPendingFanout() // let the detached fanout mark the responders

	code, resp := getActivity(t, srv, "group:planning")
	assert.Equal(t, http.StatusOK, code)
	assert.Equal(t, []string{"crimson-fox", "ember-owl"}, resp.Thinking)
}

func TestChannelActivityEndpoint_ClearsOnReply(t *testing.T) {
	srv, _, router := activityTestServer(t)
	createActivityChannel(t, srv, router)

	pub, _ := json.Marshal(publishMessageRequest{SenderID: "alice", Content: "standup time"})
	require.Equal(t, http.StatusCreated,
		doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels/group:planning/messages", pub).Code)
	router.WaitForPendingFanout()

	// ember-owl answers — its reply re-enters via the same publish path and
	// clears it from the thinking set.
	reply, _ := json.Marshal(publishMessageRequest{SenderID: "ember-owl", Content: "here's my read"})
	require.Equal(t, http.StatusCreated,
		doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels/group:planning/messages", reply).Code)
	router.WaitForPendingFanout()

	_, resp := getActivity(t, srv, "group:planning")
	assert.Equal(t, []string{"crimson-fox"}, resp.Thinking)
}

func TestChannelActivityEndpoint_IdleChannelIsEmptyArray(t *testing.T) {
	srv, _, router := activityTestServer(t)
	createActivityChannel(t, srv, router)

	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/channels/group:planning/activity", nil)
	require.Equal(t, http.StatusOK, rec.Code)
	// An idle channel marshals as [], never null, so the console needs no
	// special case for "no one thinking".
	assert.JSONEq(t, `{"thinking":[]}`, rec.Body.String())
}

func TestChannelActivityEndpoint_UnknownChannelNotFound(t *testing.T) {
	srv, _, _ := activityTestServer(t)
	code, _ := getActivity(t, srv, "group:nope")
	assert.Equal(t, http.StatusNotFound, code)
}
