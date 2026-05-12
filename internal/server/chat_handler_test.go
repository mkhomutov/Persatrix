package server

import (
	"context"
	"encoding/json"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/channels"
	"github.com/mkhomutov/persatrix/internal/planner"
	"github.com/mkhomutov/persatrix/internal/registry"
	"github.com/mkhomutov/persatrix/internal/state"
)

// chatTestServer wires a real on-disk SQLite channels store + router
// onto a fresh test Server. The router uses the no-op dispatcher (gRPC
// fanout is exercised by the channels package's own tests). The chat
// handler exercises the in-process publish-and-await flow; tests
// simulate the agent's reply by calling [channels.ChannelRouter.Publish]
// from a goroutine after the handler issues the request.
//
// RFC 0011 PR 4a-ii-β-2 — replaces the pre-rewrite mockChatExecutor
// fixture. The chat surface no longer round-trips through the
// agent-side gRPC `SendChatMessage`; it publishes a CHANNEL_MESSAGE on
// the canonical DM channel and awaits the agent's reply.
func chatTestServer(t *testing.T) (*Server, *registry.InMemoryRegistry, *channels.ChannelRouter, channels.ChannelStore) {
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
	reg := registry.NewInMemoryRegistry(logger)
	srv, err := New("127.0.0.1:0", wfDir,
		state.NewInMemoryStore(logger),
		reg,
		planner.NewYAMLPlanner(logger),
		logger,
		WithChannels(store, router),
	)
	require.NoError(t, err)
	return srv, reg, router, store
}

// registerHealthyAgent registers a healthy agent with a display name.
func registerHealthyAgent(t *testing.T, reg *registry.InMemoryRegistry, id, name string) {
	t.Helper()
	require.NoError(t, reg.Register(context.Background(), registry.AgentInfo{
		ID:      id,
		Name:    name,
		Address: "localhost:9090",
		Status:  registry.StatusHealthy,
	}))
}

// publishReplyAfter simulates the agent's `SEND_CHANNEL_MESSAGE` arrival
// on the DM by publishing through the router from the agent's id after
// `delay`. Returns the published message id so the caller can correlate.
func publishReplyAfter(t *testing.T, router *channels.ChannelRouter, store channels.ChannelStore,
	userID, agentID, content string, delay time.Duration) {
	t.Helper()
	go func() {
		time.Sleep(delay)
		dm, err := store.GetOrCreateDM(context.Background(), userID, agentID)
		if err != nil {
			t.Errorf("publishReplyAfter: GetOrCreateDM failed: %v", err)
			return
		}
		if err := router.Publish(context.Background(), channels.ChannelMessage{
			ID:        uuid.NewString(),
			ChannelID: dm.ID,
			SenderID:  agentID,
			Content:   content,
			Timestamp: time.Now().UTC(),
		}, ""); err != nil {
			t.Errorf("publishReplyAfter: Publish failed: %v", err)
		}
	}()
}

// TestHandleChat_Success_RoutesViaChannels pins the chat-as-DM happy
// path: a chat request publishes onto the DM, the agent's simulated
// reply arrives via the publish path, and the handler returns the
// reply with the agent's display name.
func TestHandleChat_Success_RoutesViaChannels(t *testing.T) {
	srv, reg, router, store := chatTestServer(t)
	registerHealthyAgent(t, reg, "ember-owl", "Ember Owl")

	publishReplyAfter(t, router, store, "alice", "ember-owl", "Hello from Ember Owl", 20*time.Millisecond)

	body, _ := json.Marshal(chatRequest{Message: "Hi there!", UserID: "alice"})
	rec := doRequest(srv.Handler(), "POST", "/api/v1/agents/ember-owl/chat", body)
	require.Equal(t, 200, rec.Code, "body=%s", rec.Body.String())

	var resp chatResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	assert.Equal(t, "Hello from Ember Owl", resp.Reply)
	assert.Equal(t, "ember-owl", resp.AgentID)
	assert.Equal(t, "Ember Owl", resp.AgentDisplayName)
	assert.Equal(t, "ok", resp.ReplyStatus)
	assert.NotEmpty(t, resp.ChatSessionID, "handler must mint a chat session id when none supplied")

	// Verify the DM was created and both messages persisted.
	dm, err := store.GetOrCreateDM(context.Background(), "alice", "ember-owl")
	require.NoError(t, err)
	hist, err := store.GetHistory(context.Background(), dm.ID, 10, time.Time{})
	require.NoError(t, err)
	require.Len(t, hist, 2, "inbound + reply both persisted")
}

// TestHandleChat_AgentDisplayNameFallsBackToID pins the §C contract
// that the response carries the agent ID when the registry has no
// display name.
func TestHandleChat_AgentDisplayNameFallsBackToID(t *testing.T) {
	srv, reg, router, store := chatTestServer(t)
	registerHealthyAgent(t, reg, "no-name", "")
	publishReplyAfter(t, router, store, "alice", "no-name", "ok", 10*time.Millisecond)

	body, _ := json.Marshal(chatRequest{Message: "Hi", UserID: "alice"})
	rec := doRequest(srv.Handler(), "POST", "/api/v1/agents/no-name/chat", body)
	require.Equal(t, 200, rec.Code)

	var resp chatResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	assert.Equal(t, "no-name", resp.AgentDisplayName)
}

// TestHandleChat_PreservesChatSessionID pins that a client-supplied
// chat_session_id round-trips unchanged.
func TestHandleChat_PreservesChatSessionID(t *testing.T) {
	srv, reg, router, store := chatTestServer(t)
	registerHealthyAgent(t, reg, "agent-x", "Agent X")
	publishReplyAfter(t, router, store, "alice", "agent-x", "ok", 10*time.Millisecond)

	body, _ := json.Marshal(chatRequest{Message: "Hi", UserID: "alice", ChatSessionID: "sess-abc"})
	rec := doRequest(srv.Handler(), "POST", "/api/v1/agents/agent-x/chat", body)
	require.Equal(t, 200, rec.Code)

	var resp chatResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	assert.Equal(t, "sess-abc", resp.ChatSessionID)
}

// TestHandleChat_EmptyMessage pins request validation.
func TestHandleChat_EmptyMessage(t *testing.T) {
	srv, reg, _, _ := chatTestServer(t)
	registerHealthyAgent(t, reg, "agent-x", "Agent X")
	body, _ := json.Marshal(chatRequest{Message: "", UserID: "alice"})
	rec := doRequest(srv.Handler(), "POST", "/api/v1/agents/agent-x/chat", body)
	assert.Equal(t, 400, rec.Code)
}

// TestHandleChat_OversizedMessage pins the 4000-char cap.
func TestHandleChat_OversizedMessage(t *testing.T) {
	srv, reg, _, _ := chatTestServer(t)
	registerHealthyAgent(t, reg, "agent-x", "Agent X")
	body, _ := json.Marshal(chatRequest{
		Message: strings.Repeat("a", chatMaxMessageLength+1),
		UserID:  "alice",
	})
	rec := doRequest(srv.Handler(), "POST", "/api/v1/agents/agent-x/chat", body)
	assert.Equal(t, 400, rec.Code)
}

// TestHandleChat_AgentNotFound pins 404 when the agent is not in the
// registry — handler short-circuits before opening a DM row.
func TestHandleChat_AgentNotFound(t *testing.T) {
	srv, _, _, store := chatTestServer(t)
	body, _ := json.Marshal(chatRequest{Message: "Hi", UserID: "alice"})
	rec := doRequest(srv.Handler(), "POST", "/api/v1/agents/unknown-agent/chat", body)
	assert.Equal(t, 404, rec.Code)

	// No DM should have been created for a 404.
	_, err := store.GetChannel(context.Background(), "dm:alice:unknown-agent")
	assert.ErrorIs(t, err, channels.ErrChannelNotFound, "404 must not create a DM row")
}

// TestHandleChat_AgentNotHealthy pins 503 when the registry reports
// the agent is non-healthy (quarantined / starting / etc.).
func TestHandleChat_AgentNotHealthy(t *testing.T) {
	srv, reg, _, _ := chatTestServer(t)
	require.NoError(t, reg.Register(context.Background(), registry.AgentInfo{
		ID: "sick-agent", Name: "Sick", Address: "localhost:9090",
		Status: registry.StatusOffline,
	}))

	body, _ := json.Marshal(chatRequest{Message: "Hi", UserID: "alice"})
	rec := doRequest(srv.Handler(), "POST", "/api/v1/agents/sick-agent/chat", body)
	assert.Equal(t, 503, rec.Code)
	assert.Contains(t, rec.Body.String(), "not healthy")
}

// TestHandleChat_ReplyTimeout pins 504 when no agent reply arrives
// within the chat timeout.
func TestHandleChat_ReplyTimeout(t *testing.T) {
	srv, reg, _, store := chatTestServer(t)
	registerHealthyAgent(t, reg, "slow-agent", "Slow")

	body, _ := json.Marshal(chatRequest{
		Message: "Hi", UserID: "alice", TimeoutSeconds: 1,
	})
	rec := doRequest(srv.Handler(), "POST", "/api/v1/agents/slow-agent/chat", body)
	assert.Equal(t, 504, rec.Code)
	assert.Contains(t, rec.Body.String(), "did not respond")

	// Inbound message persists despite timeout — RFC 0011 PR 4a-ii-β-2
	// contract: the user's turn is not lost.
	dm, err := store.GetOrCreateDM(context.Background(), "alice", "slow-agent")
	require.NoError(t, err)
	hist, err := store.GetHistory(context.Background(), dm.ID, 10, time.Time{})
	require.NoError(t, err)
	require.Len(t, hist, 1, "inbound persists even on chat timeout")
	assert.Equal(t, "Hi", hist[0].Content)
}

// TestHandleChat_WrongContentType pins the JSON-only contract.
func TestHandleChat_WrongContentType(t *testing.T) {
	srv, _, _, _ := chatTestServer(t)
	req := httptest.NewRequest("POST", "/api/v1/agents/agent-x/chat", strings.NewReader(`{}`))
	req.Header.Set("Content-Type", "text/plain")
	rec := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, req)
	assert.Equal(t, 400, rec.Code)
	assert.Contains(t, rec.Body.String(), "Content-Type must be application/json")
}

// TestHandleChat_NoChannelsConfigured pins 500 "chat not available"
// when the channels subsystem is not wired (matches pre-rewrite
// behaviour where `WithChatExecutor` was the gating option).
func TestHandleChat_NoChannelsConfigured(t *testing.T) {
	dir := t.TempDir()
	logger := zap.NewNop()
	st := state.NewInMemoryStore(logger)
	reg := registry.NewInMemoryRegistry(logger)
	pl := planner.NewYAMLPlanner(logger)
	srv, err := New("127.0.0.1:0", dir, st, reg, pl, logger)
	require.NoError(t, err)

	registerHealthyAgent(t, reg, "agent-x", "Agent X")

	body, _ := json.Marshal(chatRequest{Message: "Hi", UserID: "alice"})
	rec := doRequest(srv.Handler(), "POST", "/api/v1/agents/agent-x/chat", body)
	assert.Equal(t, 500, rec.Code)
	assert.Contains(t, rec.Body.String(), "chat not available")
}

// TestHandleChat_DefaultUserIDForLocalChat pins the REPL behaviour:
// `persatrix chat` does not configure a user_id, so empty user_id is
// substituted with `local` so the canonical DM has a stable peer.
func TestHandleChat_DefaultUserIDForLocalChat(t *testing.T) {
	srv, reg, router, store := chatTestServer(t)
	registerHealthyAgent(t, reg, "agent-x", "Agent X")
	publishReplyAfter(t, router, store, "local", "agent-x", "ok", 10*time.Millisecond)

	body, _ := json.Marshal(chatRequest{Message: "Hi"}) // no user_id
	rec := doRequest(srv.Handler(), "POST", "/api/v1/agents/agent-x/chat", body)
	require.Equal(t, 200, rec.Code, "body=%s", rec.Body.String())

	dm, err := store.GetOrCreateDM(context.Background(), "local", "agent-x")
	require.NoError(t, err)
	assert.Equal(t, "dm:agent-x:local", dm.ID, "canonical DM for the REPL fallback peer")
}

// TestHandleChat_InvalidUserIDRejected pins that participant-id hygiene
// errors from `GetOrCreateDM` (colon, whitespace, same-as-agent) surface
// as 400 BAD_REQUEST.
func TestHandleChat_InvalidUserIDRejected(t *testing.T) {
	srv, reg, _, _ := chatTestServer(t)
	registerHealthyAgent(t, reg, "agent-x", "Agent X")

	body, _ := json.Marshal(chatRequest{Message: "Hi", UserID: "bad:user"})
	rec := doRequest(srv.Handler(), "POST", "/api/v1/agents/agent-x/chat", body)
	assert.Equal(t, 400, rec.Code)
	assert.Contains(t, rec.Body.String(), "invalid participant id")
}

// TestHandleChat_InvalidAgentIDFormat pins resourceIDRegex enforcement
// at the handler boundary — consistent with handleGetAgent /
// handleDeleteAgent.
func TestHandleChat_InvalidAgentIDFormat(t *testing.T) {
	srv, _, _, _ := chatTestServer(t)
	tests := []struct {
		name, agentID string
	}{
		{"uppercase", "Agent-X"},
		{"underscore", "agent_x"},
		{"trailing dash", "agent-"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			body, _ := json.Marshal(chatRequest{Message: "Hi", UserID: "alice"})
			rec := doRequest(srv.Handler(), "POST", "/api/v1/agents/"+tt.agentID+"/chat", body)
			assert.Equal(t, 400, rec.Code)
		})
	}
}

// TestHandleChat_TimeoutClamp pins the 1s..300s clamp on the
// caller-supplied timeout (defense-in-depth against a degenerate huge
// or negative timeout taking down the request budget).
func TestHandleChat_TimeoutClamp(t *testing.T) {
	srv, reg, router, store := chatTestServer(t)
	registerHealthyAgent(t, reg, "agent-x", "Agent X")
	// Caller asks for absurdly large timeout — handler must still
	// return promptly when the reply lands.
	publishReplyAfter(t, router, store, "alice", "agent-x", "ok", 10*time.Millisecond)

	body, _ := json.Marshal(chatRequest{
		Message: "Hi", UserID: "alice", TimeoutSeconds: 99999,
	})
	rec := doRequest(srv.Handler(), "POST", "/api/v1/agents/agent-x/chat", body)
	require.Equal(t, 200, rec.Code, "clamp must not break the happy path")
}

// TestHandleChat_TimeoutClamp_LowerBound pins that non-positive
// caller-supplied timeouts (0 or negative) fall through to the default
// rather than collapsing the wait window to zero. Only the upper
// clamp was previously exercised; this closes
// the lower-edge gap so a future regression that propagates `<= 0`
// into `time.Duration` (instant timeout) is caught.
func TestHandleChat_TimeoutClamp_LowerBound(t *testing.T) {
	for _, tc := range []struct {
		name           string
		timeoutSeconds int32
	}{
		{"zero", 0},
		{"negative", -5},
	} {
		t.Run(tc.name, func(t *testing.T) {
			srv, reg, router, store := chatTestServer(t)
			registerHealthyAgent(t, reg, "agent-x", "Agent X")
			publishReplyAfter(t, router, store, "alice", "agent-x", "ok", 10*time.Millisecond)

			body, _ := json.Marshal(chatRequest{
				Message: "Hi", UserID: "alice", TimeoutSeconds: tc.timeoutSeconds,
			})
			rec := doRequest(srv.Handler(), "POST", "/api/v1/agents/agent-x/chat", body)
			require.Equal(t, 200, rec.Code, "non-positive timeout must use the default, not 0s")
		})
	}
}

// TestHandleChat_PropagatesSessionAndParticipantMetadata pins that the
// `chat_session_id` and `participant_type` request fields are carried
// into the inbound `ChannelMessage.Metadata` map under the keys named
// in the RFC 0011 amendment §Mapping table. Without this, the wire
// fields are silently inert: callers that rely on `chat_session_id` to
// segment threads, or on `participant_type` to distinguish human vs.
// bridge senders, observe no effect.
func TestHandleChat_PropagatesSessionAndParticipantMetadata(t *testing.T) {
	srv, reg, router, store := chatTestServer(t)
	registerHealthyAgent(t, reg, "agent-x", "Agent X")
	publishReplyAfter(t, router, store, "alice", "agent-x", "ok", 20*time.Millisecond)

	body, _ := json.Marshal(chatRequest{
		Message:         "Hi",
		UserID:          "alice",
		ChatSessionID:   "sess-xyz",
		ParticipantType: "human",
	})
	rec := doRequest(srv.Handler(), "POST", "/api/v1/agents/agent-x/chat", body)
	require.Equal(t, 200, rec.Code, "body=%s", rec.Body.String())

	dm, err := store.GetOrCreateDM(context.Background(), "alice", "agent-x")
	require.NoError(t, err)
	hist, err := store.GetHistory(context.Background(), dm.ID, 10, time.Time{})
	require.NoError(t, err)
	require.Len(t, hist, 2)

	// History is returned newest-first or oldest-first depending on
	// the store; locate the inbound by sender to be order-agnostic.
	var inbound *channels.ChannelMessage
	for i := range hist {
		if hist[i].SenderID == "alice" {
			inbound = &hist[i]
			break
		}
	}
	require.NotNil(t, inbound, "inbound message must persist")
	require.NotNil(t, inbound.Metadata, "metadata must be populated")
	assert.Equal(t, "sess-xyz", inbound.Metadata["chat_session_id"], "chat_session_id must be propagated to metadata")
	assert.Equal(t, "human", inbound.Metadata["participant_type"], "participant_type must be propagated to metadata")
}

// TestHandleChat_ZeroTimestampReplyDoesNotLeakNegativeUnix pins the
// defensive guard against a reply whose `Timestamp` is the time.Time
// zero value: `Time{}.Unix()` is a large negative number (year 1754),
// which would render as a nonsense client-visible timestamp. The
// handler must substitute `time.Now().UTC()` so the response carries
// a sane positive epoch second.
func TestHandleChat_ZeroTimestampReplyDoesNotLeakNegativeUnix(t *testing.T) {
	srv, reg, router, store := chatTestServer(t)
	registerHealthyAgent(t, reg, "agent-x", "Agent X")

	// Publish a reply with explicit zero timestamp.
	go func() {
		time.Sleep(20 * time.Millisecond)
		dm, err := store.GetOrCreateDM(context.Background(), "alice", "agent-x")
		if err != nil {
			t.Errorf("GetOrCreateDM failed: %v", err)
			return
		}
		_ = router.Publish(context.Background(), channels.ChannelMessage{
			ID:        uuid.NewString(),
			ChannelID: dm.ID,
			SenderID:  "agent-x",
			Content:   "ok",
			Timestamp: time.Time{}, // zero value
		}, "")
	}()

	before := time.Now().Add(-time.Second).Unix()
	body, _ := json.Marshal(chatRequest{Message: "Hi", UserID: "alice"})
	rec := doRequest(srv.Handler(), "POST", "/api/v1/agents/agent-x/chat", body)
	require.Equal(t, 200, rec.Code, "body=%s", rec.Body.String())

	var resp chatResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	assert.Greater(t, resp.Timestamp, before,
		"zero-valued reply timestamp must be replaced with current time, not surface as a negative epoch second")
}

// TestHandleChat_MultiMessageReplyReturnsFirst pins the single-shot
// semantics of `replyWaiter`: when an agent publishes multiple
// `SEND_CHANNEL_MESSAGE`s in response to a single chat turn (e.g. a
// `tool_call → tool_result → final_answer` plugin pattern), only the
// first message satisfies the waiter and is returned to the caller.
// Subsequent messages are persisted to the DM history but not
// delivered to the chat response.
func TestHandleChat_MultiMessageReplyReturnsFirst(t *testing.T) {
	srv, reg, router, store := chatTestServer(t)
	registerHealthyAgent(t, reg, "agent-x", "Agent X")

	// Publish two replies in quick succession.
	go func() {
		time.Sleep(15 * time.Millisecond)
		dm, err := store.GetOrCreateDM(context.Background(), "alice", "agent-x")
		if err != nil {
			t.Errorf("GetOrCreateDM failed: %v", err)
			return
		}
		_ = router.Publish(context.Background(), channels.ChannelMessage{
			ID:        uuid.NewString(),
			ChannelID: dm.ID,
			SenderID:  "agent-x",
			Content:   "first",
			Timestamp: time.Now().UTC(),
		}, "")
		_ = router.Publish(context.Background(), channels.ChannelMessage{
			ID:        uuid.NewString(),
			ChannelID: dm.ID,
			SenderID:  "agent-x",
			Content:   "second",
			Timestamp: time.Now().UTC(),
		}, "")
	}()

	body, _ := json.Marshal(chatRequest{Message: "Hi", UserID: "alice"})
	rec := doRequest(srv.Handler(), "POST", "/api/v1/agents/agent-x/chat", body)
	require.Equal(t, 200, rec.Code, "body=%s", rec.Body.String())

	var resp chatResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	assert.Equal(t, "first", resp.Reply, "single-shot waiter must surface the first reply")

	// Allow the second publish to complete before snapshotting.
	require.Eventually(t, func() bool {
		dm, err := store.GetOrCreateDM(context.Background(), "alice", "agent-x")
		if err != nil {
			return false
		}
		hist, err := store.GetHistory(context.Background(), dm.ID, 10, time.Time{})
		if err != nil {
			return false
		}
		// inbound + first + second = 3
		return len(hist) == 3
	}, 2*time.Second, 20*time.Millisecond, "all three messages must persist to the DM")
}
