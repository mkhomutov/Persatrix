// channel_classification_rest_test.go — RFC 0037 §B (v0.3.12 PR 2): the REST
// leg of the two-path wire contract. The history and thread envelopes carry
// the channel's §A level (the value on-startup catch-up replay stamps onto
// rebuilt events — `agents/channel_catchup.py`), and the channel object
// carries it on the list/create surface the catch-up channel walk reads.
package server

import (
	"context"
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

// TestChannels_HistoryEnvelopeCarriesClassification pins the history +
// thread envelopes: channel-level (one value per envelope, never per
// message — a message's classification IS its channel's, §H), read from the
// row the handlers already fetch.
func TestChannels_HistoryEnvelopeCarriesClassification(t *testing.T) {
	srv, store := channelTestServer(t)
	createBody, _ := json.Marshal(createChannelRequest{
		Name:    "leadership",
		Members: []channelMemberRequest{{ID: "alice"}, {ID: "bob"}},
	})
	require.Equal(t, http.StatusCreated,
		doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels", createBody).Code)
	require.NoError(t, store.SetChannelClassification(
		context.Background(), "group:leadership", channels.ClassificationRestricted))

	pubBody, _ := json.Marshal(publishMessageRequest{SenderID: "alice", Content: "quarterly plan"})
	pubRec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels/group:leadership/messages", pubBody)
	require.Equal(t, http.StatusCreated, pubRec.Code)
	var msg channelMessageResponse
	require.NoError(t, json.Unmarshal(pubRec.Body.Bytes(), &msg))

	histRec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/channels/group:leadership/messages", nil)
	require.Equal(t, http.StatusOK, histRec.Code)
	var hist historyResponse
	require.NoError(t, json.Unmarshal(histRec.Body.Bytes(), &hist))
	assert.Equal(t, "restricted", hist.Classification,
		"the history envelope must carry the channel's §A level for catch-up stamping")

	threadRec := doRequest(srv.Handler(), http.MethodGet,
		"/api/v1/channels/group:leadership/messages/"+msg.ID+"/thread", nil)
	require.Equal(t, http.StatusOK, threadRec.Code)
	var thread historyResponse
	require.NoError(t, json.Unmarshal(threadRec.Body.Bytes(), &thread))
	assert.Equal(t, "restricted", thread.Classification,
		"a thread is never more or less confidential than the channel it forks from (§B/§H)")
}

// TestChannels_ChannelObjectCarriesClassification pins the create/list
// surface: a fresh channel reads back `internal` (the §A rule-(a) stamp) —
// the catch-up channel walk and the operator opt-in path both read this
// field.
func TestChannels_ChannelObjectCarriesClassification(t *testing.T) {
	srv, _ := channelTestServer(t)
	createBody, _ := json.Marshal(createChannelRequest{
		Name:    "planning",
		Members: []channelMemberRequest{{ID: "alice"}},
	})
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels", createBody)
	require.Equal(t, http.StatusCreated, rec.Code)
	var created channelResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &created))
	assert.Equal(t, "internal", created.Classification,
		"a classification-unaware REST create stamps `internal`, and the response reads it back")

	listRec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/channels", nil)
	require.Equal(t, http.StatusOK, listRec.Code)
	var list listChannelsResponse
	require.NoError(t, json.Unmarshal(listRec.Body.Bytes(), &list))
	require.Len(t, list.Channels, 1)
	assert.Equal(t, "internal", list.Channels[0].Classification)
}

// TestChannels_DeleteEvictsDispatchClassificationCache pins the wiring half of
// the [channels.classificationCache] delete hook: DELETE is store-direct, so
// without `handleDeleteChannel`'s ForgetChannelClassification call the router
// keeps serving the deleted channel's level to every dispatch on a re-created
// id — a cached `public` over a re-created `internal` row under-classifies the
// wire, the direction that over-injects once the PR 4 gate arms.
//
// Runs the real gRPC dispatcher (like the fanout integration test) because the
// stale value is only observable on the dispatched event; NoopDispatcher would
// defeat the point.
func TestChannels_DeleteEvictsDispatchClassificationCache(t *testing.T) {
	logger := zap.NewNop()
	recBob, bobAddr, stopBob := startRecordingAgent(t)
	defer stopBob()

	reg := registry.NewInMemoryRegistry(logger)
	require.NoError(t, reg.Register(context.Background(), registry.AgentInfo{
		ID: "agent-bob", Name: "Bob", Address: bobAddr, Status: registry.StatusHealthy,
	}))

	store, err := channels.NewSQLiteStore(
		filepath.Join(t.TempDir(), "channels.db"),
		channels.SQLiteOptions{MaxChannels: 50, Logger: logger})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })

	router := channels.NewChannelRouter(store, channels.NewGRPCMessageDispatcher(reg, logger), logger, nil)
	srv, err := New("127.0.0.1:0", t.TempDir(),
		state.NewInMemoryStore(logger),
		reg,
		planner.NewYAMLPlanner(logger),
		logger,
		WithChannels(store, router),
	)
	require.NoError(t, err)

	createBody, _ := json.Marshal(createChannelRequest{
		Name: "planning",
		Members: []channelMemberRequest{
			{ID: "agent-alice", Respond: "always"},
			{ID: "agent-bob", Respond: "always"},
		},
	})
	pubBody, _ := json.Marshal(map[string]any{"sender_id": "agent-alice", "content": "hello"})

	require.Equal(t, http.StatusCreated,
		doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels", createBody).Code)
	require.NoError(t, store.SetChannelClassification(
		context.Background(), "group:planning", channels.ClassificationPublic))
	require.Equal(t, http.StatusCreated,
		doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels/group:planning/messages", pubBody).Code)
	router.WaitForPendingFanout()
	require.Len(t, recBob.snapshot(), 1)
	require.Equal(t, "public", recBob.snapshot()[0].Classification,
		"the first dispatch fills the router's read-through cache")

	require.Equal(t, http.StatusNoContent,
		doRequest(srv.Handler(), http.MethodDelete, "/api/v1/channels/group:planning", nil).Code)
	require.Equal(t, http.StatusCreated,
		doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels", createBody).Code)
	require.Equal(t, http.StatusCreated,
		doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels/group:planning/messages", pubBody).Code)
	router.WaitForPendingFanout()

	events := recBob.snapshot()
	require.Len(t, events, 2)
	assert.Equal(t, "internal", events[1].Classification,
		"the re-created channel must dispatch its own row's level, not the deleted channel's cached one")
}
