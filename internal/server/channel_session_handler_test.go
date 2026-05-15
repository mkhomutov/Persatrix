// RFC 0031 Phase 1 PR 2 — handler-level propagation of `WithChannelSessionID`.
//
// The store-level round-trip is exercised in
// `internal/channels/sqlite_session_test.go` and the boot-time env-var
// resolver in `cmd/orchestrator/session_env_test.go`. PR #335 review
// flagged a gap between those two: the Server-level wiring through
// `Server.channelSessionID` → REST handlers (`handleCreateChannel` /
// `handlePublishMessage`) and chat handler (`handleChat`) was only
// covered by code review. A refactor that drops the stamp at one of the
// three sites would have passed the existing test matrix. These tests
// pin the contract end-to-end at the HTTP boundary so a regression
// surfaces as a clean failure.
package server

import (
	"encoding/json"
	"net/http"
	"path/filepath"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/channels"
	"github.com/mkhomutov/persatrix/internal/planner"
	"github.com/mkhomutov/persatrix/internal/registry"
	"github.com/mkhomutov/persatrix/internal/state"
)

// channelSessionTestServer mirrors [channelTestServer] but threads
// `WithChannelSessionID` through so the propagation can be asserted at
// the HTTP boundary. An empty `sessionID` argument means the option is
// omitted (so the store-side `legacy` default applies — mirrors the
// orchestrator behaviour when `PERSATRIX_SESSION_ID` is unset).
func channelSessionTestServer(t *testing.T, sessionID string) (*Server, channels.ChannelStore) {
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
	opts := []ServerOption{WithChannels(store, router)}
	if sessionID != "" {
		opts = append(opts, WithChannelSessionID(sessionID))
	}
	srv, err := New("127.0.0.1:0", wfDir,
		state.NewInMemoryStore(logger),
		registry.NewInMemoryRegistry(logger),
		planner.NewYAMLPlanner(logger),
		logger,
		opts...,
	)
	require.NoError(t, err)
	return srv, store
}

// TestChannels_WithChannelSessionID_StampsCreateChannel asserts that a
// Server built with `WithChannelSessionID("run-a")` stamps `run-a` on
// every channel row created via `POST /api/v1/channels`. The check
// reads the row back from the store directly (the wire shape
// intentionally omits `session_id` per `channelToResponse`'s comment,
// so we cannot assert via the response body).
func TestChannels_WithChannelSessionID_StampsCreateChannel(t *testing.T) {
	srv, store := channelSessionTestServer(t, "run-a")

	body, _ := json.Marshal(createChannelRequest{
		Name:    "planning",
		Members: []channelMemberRequest{{ID: "alice"}},
	})
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels", body)
	require.Equal(t, http.StatusCreated, rec.Code, "body=%s", rec.Body.String())

	got, err := store.GetChannel(t.Context(), "group:planning")
	require.NoError(t, err)
	assert.Equal(t, "run-a", got.SessionID,
		"WithChannelSessionID must propagate through handleCreateChannel")
}

// TestChannels_WithChannelSessionID_StampsPublishMessage asserts that a
// Server built with `WithChannelSessionID("run-a")` stamps `run-a` on
// every message row created via `POST /api/v1/channels/{id}/messages`.
// The channel itself is created in the same test (so it also carries
// `run-a`) but the assertion is specifically on the message row — that
// is the path that runs through `handlePublishMessage` rather than
// `handleCreateChannel`, exercising the second of the three propagation
// sites.
func TestChannels_WithChannelSessionID_StampsPublishMessage(t *testing.T) {
	srv, store := channelSessionTestServer(t, "run-a")

	createBody, _ := json.Marshal(createChannelRequest{
		Name:    "planning",
		Members: []channelMemberRequest{{ID: "alice"}},
	})
	require.Equal(t, http.StatusCreated,
		doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels", createBody).Code)

	pubBody, _ := json.Marshal(publishMessageRequest{
		SenderID: "alice", Content: "hello",
	})
	rec := doRequest(srv.Handler(), http.MethodPost,
		"/api/v1/channels/group:planning/messages", pubBody)
	require.Equal(t, http.StatusCreated, rec.Code, "body=%s", rec.Body.String())

	hist, err := store.GetHistory(t.Context(), "group:planning", 10, time.Time{})
	require.NoError(t, err)
	require.Len(t, hist, 1)
	assert.Equal(t, "run-a", hist[0].SessionID,
		"WithChannelSessionID must propagate through handlePublishMessage")
}

// TestChannels_WithoutChannelSessionID_DefaultsToLegacy asserts the
// nil-safe degradation: a Server without `WithChannelSessionID` (the
// shape used by every existing test fixture and by orchestrators that
// boot with `PERSATRIX_SESSION_ID` unset) leaves
// `Server.channelSessionID == ""`, which the store rewrites to the
// `legacy` carve-out at the boundary. Pins that the missing option
// stays a soft fallback rather than a panic / 500 surface.
func TestChannels_WithoutChannelSessionID_DefaultsToLegacy(t *testing.T) {
	srv, store := channelSessionTestServer(t, "")

	body, _ := json.Marshal(createChannelRequest{
		Name:    "planning",
		Members: []channelMemberRequest{{ID: "alice"}},
	})
	require.Equal(t, http.StatusCreated,
		doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels", body).Code)

	pubBody, _ := json.Marshal(publishMessageRequest{
		SenderID: "alice", Content: "hi",
	})
	require.Equal(t, http.StatusCreated,
		doRequest(srv.Handler(), http.MethodPost,
			"/api/v1/channels/group:planning/messages", pubBody).Code)

	ch, err := store.GetChannel(t.Context(), "group:planning")
	require.NoError(t, err)
	assert.Equal(t, channels.DefaultSessionID, ch.SessionID,
		"missing option leaves the store-side legacy default in effect")

	hist, err := store.GetHistory(t.Context(), "group:planning", 10, time.Time{})
	require.NoError(t, err)
	require.Len(t, hist, 1)
	assert.Equal(t, channels.DefaultSessionID, hist[0].SessionID,
		"missing option leaves the store-side legacy default in effect for messages")
}
