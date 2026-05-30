// RFC 0031 Phase 3 PR 1 — `/api/v1/sessions` REST surface.
//
// These tests pin the orchestrator-side session registry endpoints
// (create / list / get / archive) at the HTTP boundary. There is no CLI
// caller yet — PR 2 adds `persatrix session …` against these routes — so
// this surface ships as a pure enabler with no operator-visible behaviour
// change (it returns 503 when channels are not wired).
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

// sessionTestServer builds a Server wired with a real SQLite-backed channel
// store (so the session registry is live) and returns both the server and the
// store so tests can seed auto-minted sessions via the resolver. Mirrors
// channelSessionTestServer but returns the concrete store for resolver access.
func sessionTestServer(t *testing.T) (*Server, channels.ChannelStore) {
	t.Helper()
	dbPath := filepath.Join(t.TempDir(), "channels.db")
	store, err := channels.NewSQLiteStore(dbPath, channels.SQLiteOptions{
		MaxChannels: 50,
		Logger:      zap.NewNop(),
	})
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
	return srv, store
}

// createSessionViaREST is a small helper that POSTs a create and returns the
// decoded response, asserting a 201.
func createSessionViaREST(t *testing.T, srv *Server, label string) sessionResponse {
	t.Helper()
	body, _ := json.Marshal(createSessionRequest{Label: label})
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/sessions", body)
	require.Equal(t, http.StatusCreated, rec.Code, "body=%s", rec.Body.String())
	var resp sessionResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	return resp
}

// TestSessions_CreateListRoundTrip asserts a created session surfaces in the
// list response — the core create→list contract PR 2's `session new`/`list`
// verbs depend on.
func TestSessions_CreateListRoundTrip(t *testing.T) {
	srv, _ := sessionTestServer(t)

	created := createSessionViaREST(t, srv, "arc-1")
	assert.NotEmpty(t, created.ID)
	assert.Equal(t, "arc-1", created.Label)
	assert.False(t, created.Archived)

	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/sessions", nil)
	require.Equal(t, http.StatusOK, rec.Code)
	var list listSessionsResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &list))
	require.Len(t, list.Sessions, 1)
	assert.Equal(t, created.ID, list.Sessions[0].ID)
	assert.Equal(t, "arc-1", list.Sessions[0].Label)
}

// TestSessions_ListFiltersArchived asserts the default list omits archived
// sessions and `?include_archived=true` includes them.
func TestSessions_ListFiltersArchived(t *testing.T) {
	srv, _ := sessionTestServer(t)

	active := createSessionViaREST(t, srv, "active")
	archived := createSessionViaREST(t, srv, "to-archive")

	rec := doRequest(srv.Handler(), http.MethodPost,
		"/api/v1/sessions/"+archived.ID+"/archive", nil)
	require.Equal(t, http.StatusNoContent, rec.Code, "body=%s", rec.Body.String())

	// Default: active only.
	rec = doRequest(srv.Handler(), http.MethodGet, "/api/v1/sessions", nil)
	require.Equal(t, http.StatusOK, rec.Code)
	var def listSessionsResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &def))
	require.Len(t, def.Sessions, 1)
	assert.Equal(t, active.ID, def.Sessions[0].ID)

	// include_archived=true: both.
	rec = doRequest(srv.Handler(), http.MethodGet, "/api/v1/sessions?include_archived=true", nil)
	require.Equal(t, http.StatusOK, rec.Code)
	var all listSessionsResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &all))
	require.Len(t, all.Sessions, 2)
}

// TestSessions_ArchivePreservesResolvableRow asserts archive flips the flag
// without deleting the row — a GET still resolves it (and its tagged memory
// stays reachable via the legacy/recall path).
func TestSessions_ArchivePreservesResolvableRow(t *testing.T) {
	srv, _ := sessionTestServer(t)
	created := createSessionViaREST(t, srv, "arc")

	rec := doRequest(srv.Handler(), http.MethodPost,
		"/api/v1/sessions/"+created.ID+"/archive", nil)
	require.Equal(t, http.StatusNoContent, rec.Code)

	rec = doRequest(srv.Handler(), http.MethodGet, "/api/v1/sessions/"+created.ID, nil)
	require.Equal(t, http.StatusOK, rec.Code, "archived session is still resolvable")
	var got sessionResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &got))
	assert.True(t, got.Archived)
	assert.Equal(t, created.ID, got.ID)
}

// TestSessions_GetResolvesByIDAndLabel asserts GET /{id} accepts both the id
// and the label as the path key — the contract `session use`/`current` label
// rendering depends on.
func TestSessions_GetResolvesByIDAndLabel(t *testing.T) {
	srv, _ := sessionTestServer(t)
	created := createSessionViaREST(t, srv, "by-label")

	recByID := doRequest(srv.Handler(), http.MethodGet, "/api/v1/sessions/"+created.ID, nil)
	require.Equal(t, http.StatusOK, recByID.Code)
	var byID sessionResponse
	require.NoError(t, json.Unmarshal(recByID.Body.Bytes(), &byID))
	assert.Equal(t, created.ID, byID.ID)

	recByLabel := doRequest(srv.Handler(), http.MethodGet, "/api/v1/sessions/by-label", nil)
	require.Equal(t, http.StatusOK, recByLabel.Code)
	var byLabel sessionResponse
	require.NoError(t, json.Unmarshal(recByLabel.Body.Bytes(), &byLabel))
	assert.Equal(t, created.ID, byLabel.ID, "label resolves to the same row as the id")
}

// TestSessions_GetUnknownReturns404 asserts an unknown id-or-label 404s.
func TestSessions_GetUnknownReturns404(t *testing.T) {
	srv, _ := sessionTestServer(t)
	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/sessions/no-such-session", nil)
	assert.Equal(t, http.StatusNotFound, rec.Code)
}

// TestSessions_CreateReservedLegacyRejected pins OQ #2a at the REST boundary:
// `legacy` as a label is a 4xx the CLI can surface verbatim, server-side and
// authoritative so a direct REST caller cannot mint a colliding row.
func TestSessions_CreateReservedLegacyRejected(t *testing.T) {
	srv, _ := sessionTestServer(t)

	body, _ := json.Marshal(createSessionRequest{Label: channels.DefaultSessionID})
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/sessions", body)
	assert.Equal(t, http.StatusBadRequest, rec.Code, "reserved legacy label must be rejected")

	// No row leaked.
	rec = doRequest(srv.Handler(), http.MethodGet, "/api/v1/sessions?include_archived=true", nil)
	require.Equal(t, http.StatusOK, rec.Code)
	var all listSessionsResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &all))
	assert.Empty(t, all.Sessions)
}

// TestSessions_CreateEmptyLabelRejected asserts a missing label is a 400 —
// an unnamed operator session is a caller bug (the auto-mint path is the only
// route that creates label-less rows).
func TestSessions_CreateEmptyLabelRejected(t *testing.T) {
	srv, _ := sessionTestServer(t)
	body, _ := json.Marshal(createSessionRequest{Label: ""})
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/sessions", body)
	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

// TestSessions_CreateWhitespaceLabelRejected asserts a whitespace-only label
// is rejected like an empty one — "   " is not a name, and the boundary trims
// before the required-label check.
func TestSessions_CreateWhitespaceLabelRejected(t *testing.T) {
	srv, _ := sessionTestServer(t)
	body, _ := json.Marshal(createSessionRequest{Label: "   "})
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/sessions", body)
	assert.Equal(t, http.StatusBadRequest, rec.Code,
		"a whitespace-only label must be rejected like an empty one")

	// No row leaked.
	rec = doRequest(srv.Handler(), http.MethodGet, "/api/v1/sessions?include_archived=true", nil)
	require.Equal(t, http.StatusOK, rec.Code)
	var all listSessionsResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &all))
	assert.Empty(t, all.Sessions)
}

// TestSessions_CreateTrimsLabelWhitespace asserts surrounding whitespace is
// trimmed at the boundary so the stored (and rendered) label is canonical.
func TestSessions_CreateTrimsLabelWhitespace(t *testing.T) {
	srv, _ := sessionTestServer(t)
	created := createSessionViaREST(t, srv, "  arc-1  ")
	assert.Equal(t, "arc-1", created.Label,
		"surrounding whitespace is trimmed so the stored label is canonical")
}

// TestSessions_AutoMintedAppearsInList asserts a session minted by the
// ISSUE-0082 dispatch-path resolver (seeded here directly) is visible through
// the operator-facing list endpoint on day one.
func TestSessions_AutoMintedAppearsInList(t *testing.T) {
	srv, store := sessionTestServer(t)
	resolver, err := channels.NewSessionResolver(store)
	require.NoError(t, err)
	autoID, err := resolver.Resolve(t.Context(), "agent-a", "group:planning", "user-1")
	require.NoError(t, err)

	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/sessions", nil)
	require.Equal(t, http.StatusOK, rec.Code)
	var list listSessionsResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &list))
	require.Len(t, list.Sessions, 1)
	assert.Equal(t, autoID, list.Sessions[0].ID)
}

// TestSessions_ServiceUnavailableWhenChannelsUnwired asserts the endpoints
// degrade to 503 (not panic / 500) when the channel store — and therefore the
// session registry — is not configured, matching the channel handlers' shape.
func TestSessions_ServiceUnavailableWhenChannelsUnwired(t *testing.T) {
	logger := zap.NewNop()
	srv, err := New("127.0.0.1:0", t.TempDir(),
		state.NewInMemoryStore(logger),
		registry.NewInMemoryRegistry(logger),
		planner.NewYAMLPlanner(logger),
		logger,
	)
	require.NoError(t, err)

	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/sessions", nil)
	assert.Equal(t, http.StatusServiceUnavailable, rec.Code)
}
