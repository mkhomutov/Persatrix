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

// replyBudgetTestServer wires a server whose channel router exposes a reply
// budget the test can set, so the RFC 0030 Layer 2 → HTTP 429 boundary can be
// exercised end-to-end. Returns the router so the test can call SetReplyBudget.
func replyBudgetTestServer(t *testing.T) (*Server, *channels.ChannelRouter) {
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
	return srv, router
}

func mustCreateChannelHTTP(t *testing.T, srv *Server, name string, members ...string) {
	t.Helper()
	reqMembers := make([]channelMemberRequest, len(members))
	for i, m := range members {
		reqMembers[i] = channelMemberRequest{ID: m, Respond: "always"}
	}
	body, _ := json.Marshal(createChannelRequest{Name: name, Members: reqMembers})
	require.Equal(t, http.StatusCreated,
		doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels", body).Code)
}

func publishHTTP(t *testing.T, srv *Server, channelID, sender, interactionID string) int {
	t.Helper()
	body, _ := json.Marshal(publishMessageRequest{
		SenderID: sender,
		Content:  "msg",
		Metadata: map[string]any{"interaction_id": interactionID},
	})
	return doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels/"+channelID+"/messages", body).Code
}

// TestChannels_ReplyBudget_KPlusOne_429 pins the RFC 0030 Layer 2 →
// HTTP 429 boundary: with K=2, a participant's 3rd publish in one interaction
// is rejected with 429 (ErrParticipantBudgetExhausted → TOO_MANY_REQUESTS) and
// never enters channel history.
func TestChannels_ReplyBudget_KPlusOne_429(t *testing.T) {
	srv, router := replyBudgetTestServer(t)
	mustCreateChannelHTTP(t, srv, "planning", "alice", "bob")
	router.SetReplyBudget("group:planning", 2)

	assert.Equal(t, http.StatusCreated, publishHTTP(t, srv, "group:planning", "alice", "int-1"))
	assert.Equal(t, http.StatusCreated, publishHTTP(t, srv, "group:planning", "alice", "int-1"))
	// The (K+1)th is rejected with 429.
	assert.Equal(t, http.StatusTooManyRequests, publishHTTP(t, srv, "group:planning", "alice", "int-1"))

	// Pre-persistence: only the two accepted messages are in history.
	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/channels/group:planning/messages", nil)
	require.Equal(t, http.StatusOK, rec.Code)
	var resp struct {
		Messages []json.RawMessage `json:"messages"`
	}
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	assert.Len(t, resp.Messages, 2, "the rejected (K+1)th publish must not be persisted")
}

// TestChannels_ReplyBudget_DefaultUncapped pins the opt-in default: with no
// budget set, a participant publishes past any cap with 201s (v0.3.0 behaviour).
func TestChannels_ReplyBudget_DefaultUncapped(t *testing.T) {
	srv, _ := replyBudgetTestServer(t)
	mustCreateChannelHTTP(t, srv, "planning", "alice", "bob")

	for i := 0; i < 5; i++ {
		assert.Equal(t, http.StatusCreated, publishHTTP(t, srv, "group:planning", "alice", "int-1"))
	}
}
