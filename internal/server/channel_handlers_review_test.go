package server

import (
	"encoding/json"
	"net/http"
	"path/filepath"
	"sync"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
	"go.uber.org/zap/zaptest/observer"

	"github.com/mkhomutov/persatrix/internal/channels"
	"github.com/mkhomutov/persatrix/internal/planner"
	"github.com/mkhomutov/persatrix/internal/registry"
	"github.com/mkhomutov/persatrix/internal/state"
)

// PR #245 review fix-ups — handler-layer hardening.
//
// These tests pin three review findings against the channel REST surface:
//
//  1. respond-policy boundary validation (Should-Fix #4): an unknown
//     `respond:` value in CreateChannel / AddMember should be rejected
//     at the handler with a clean 400, not delegated to the store CHECK
//     constraint (which surfaces as 500 INTERNAL).
//  2. parseLimit strictness (Low): a malformed `?limit=` query parameter
//     should surface as 400, not silently fall back to the default —
//     malformed values are bugs we want clients to fix, not paper over.
//  3. atomic create-then-add-members (High): a partial-failure mid-loop
//     in handleCreateChannel must NOT leave an orphan channel that
//     poisons the client's retry with 409 CONFLICT. The new
//     `CreateChannelWithMembers` store path makes the create+add atomic;
//     this test pins the externally observable behaviour.

// TestChannels_CreateChannel_RejectsUnknownRespondPolicy pins finding
// "validate respond at REST boundary": invalid `respond` strings should
// return 400 with a typed error, not 500 from the store CHECK constraint.
func TestChannels_CreateChannel_RejectsUnknownRespondPolicy(t *testing.T) {
	srv, _ := channelTestServer(t)
	body, _ := json.Marshal(createChannelRequest{
		Name: "planning",
		Members: []channelMemberRequest{
			{ID: "alice", Respond: "always"},
			{ID: "bob", Respond: "occasionally"}, // invalid
		},
	})
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels", body)
	assert.Equal(t, http.StatusBadRequest, rec.Code,
		"unknown respond policy must surface as 400 from the boundary, not 500 from the store")
}

// TestChannels_AddMember_RejectsUnknownRespondPolicy pins the same
// contract on the add-member endpoint.
func TestChannels_AddMember_RejectsUnknownRespondPolicy(t *testing.T) {
	srv, _ := channelTestServer(t)
	createBody, _ := json.Marshal(createChannelRequest{
		Name:    "planning",
		Members: []channelMemberRequest{{ID: "alice"}},
	})
	require.Equal(t, http.StatusCreated,
		doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels", createBody).Code)

	addBody, _ := json.Marshal(addMemberRequest{ID: "carol", Respond: "sometimes"})
	rec := doRequest(srv.Handler(), http.MethodPost,
		"/api/v1/channels/group:planning/members", addBody)
	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

// TestChannels_ListChannels_MalformedLimit_400 pins finding "tighten
// parseLimit": a non-empty malformed `?limit=` value should reject with
// 400, not silently fall back to the default.
func TestChannels_ListChannels_MalformedLimit_400(t *testing.T) {
	srv, _ := channelTestServer(t)

	cases := []struct {
		name string
		raw  string
	}{
		{"non-numeric", "?limit=abc"},
		{"negative", "?limit=-5"},
		{"zero", "?limit=0"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/channels"+c.raw, nil)
			assert.Equal(t, http.StatusBadRequest, rec.Code,
				"malformed limit %q must surface as 400", c.raw)
		})
	}

	// Sanity: an absent limit still works (falls back to the default).
	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/channels", nil)
	assert.Equal(t, http.StatusOK, rec.Code)
}

// TestChannels_CreateChannel_AtomicOnInvalidMember pins finding
// "non-atomic create-then-add-members": an invalid member id mid-list
// must NOT leave an orphan channel that poisons the client's retry.
//
// Pre-fix behaviour: handleCreateChannel called CreateChannel, then
// looped per-member calling AddMember; an invalid id in the second slot
// returned 400/500 with the channel already created. A client retry
// then hit ErrChannelExists → 409, and the second member was never
// added. Post-fix: the handler routes through the atomic
// CreateChannelWithMembers store helper, which rolls back the channel
// row when any membership insert fails, so the retry succeeds cleanly.
func TestChannels_CreateChannel_AtomicOnInvalidMember(t *testing.T) {
	srv, _ := channelTestServer(t)
	body, _ := json.Marshal(createChannelRequest{
		Name: "planning",
		Members: []channelMemberRequest{
			{ID: "alice"},
			{ID: ""}, // invalid → triggers rollback in the store
		},
	})
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels", body)
	require.NotEqual(t, http.StatusCreated, rec.Code,
		"invalid member must reject the entire create")

	// Channel must NOT exist. If the rollback worked, GET returns 404.
	getRec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/channels/group:planning", nil)
	assert.Equal(t, http.StatusNotFound, getRec.Code,
		"failed create must not leak an orphan channel row")

	// Retry with a clean member list succeeds — no ErrChannelExists from
	// a leaked row.
	cleanBody, _ := json.Marshal(createChannelRequest{
		Name:    "planning",
		Members: []channelMemberRequest{{ID: "alice"}},
	})
	retryRec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels", cleanBody)
	assert.Equal(t, http.StatusCreated, retryRec.Code,
		"retry after rolled-back create must succeed (no 409)")
}

// TestChannels_CreateChannel_InvalidName_400 pins PR #245 re-review
// finding (Low/Med): a `name` value that fails channelNamePattern at
// the store boundary used to surface as 500 INTERNAL because the
// store's name-pattern error did not wrap any of the typed sentinels
// that writeChannelError understands. The store wrapping fix
// reclassifies it as ErrInvalidChannelType so the user-visible status
// is the correct 400 BAD_REQUEST.
//
// Cases cover the three failure shapes operators are likely to type:
// uppercase letters, embedded whitespace, and path-traversal-style
// punctuation. All three must surface as 400 with a message naming
// the offending value (no internal sentinel leak).
func TestChannels_CreateChannel_InvalidName_400(t *testing.T) {
	cases := []struct {
		name    string
		reqName string
	}{
		{"uppercase", "Planning"},
		{"whitespace", "sprint 1"},
		{"path-traversal", "evil/../path"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			srv, _ := channelTestServer(t)
			body, _ := json.Marshal(createChannelRequest{
				Name:    c.reqName,
				Members: []channelMemberRequest{{ID: "alice"}},
			})
			rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/channels", body)
			assert.Equal(t, http.StatusBadRequest, rec.Code,
				"invalid name %q must surface as 400, not 500", c.reqName)
		})
	}
}

// channelTestServerNoRouter wires the channel store WITHOUT a router so
// the publish handler exercises the direct-store fallback path.
// Returns the observer alongside the server so the test can assert log
// emission. The contract being pinned is the once-per-process Warn
// added in PR #245 review (round 3) Should-Fix #3.
func channelTestServerNoRouter(t *testing.T) (*Server, *observer.ObservedLogs) {
	t.Helper()
	dbPath := filepath.Join(t.TempDir(), "channels.db")
	store, err := channels.NewSQLiteStore(dbPath, channels.SQLiteOptions{
		MaxChannels: 50,
		Logger:      zap.NewNop(),
	})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })

	core, recorded := observer.New(zap.WarnLevel)
	logger := zap.New(core)

	wfDir := t.TempDir()
	srv, err := New("127.0.0.1:0", wfDir,
		state.NewInMemoryStore(logger),
		registry.NewInMemoryRegistry(logger),
		planner.NewYAMLPlanner(logger),
		logger,
		WithChannels(store, nil), // intentionally nil router → fallback path
	)
	require.NoError(t, err)
	return srv, recorded
}

// TestChannels_PublishMessage_RouterNilFallback_LogsWarnOnce pins
// PR #245 review (round 3) Should-Fix #3. The router-nil branch in
// handlePublishMessage previously bypassed channel_type cross-validation
// and the channel.messages.delivered metric without any log line, so a
// production misconfiguration (forgetting to wire the router via
// WithChannels) would silently degrade observability AND skip a
// validation step. The fix emits a once-per-process Warn so:
//
//   - the misconfiguration is visible in ops logs at startup-traffic
//     time without needing to scrape metrics;
//   - publish remains a hot path (per-request Warn would flood logs).
//
// The "exactly once" property is the contract this test pins.
//
// NOTE: shares the package-level channelFallbackWarnOnce with other
// tests in this package — this test resets it explicitly so order is
// not significant.
func TestChannels_PublishMessage_RouterNilFallback_LogsWarnOnce(t *testing.T) {
	channelFallbackWarnOnce = sync.Once{}
	t.Cleanup(func() { channelFallbackWarnOnce = sync.Once{} })

	srv, recorded := channelTestServerNoRouter(t)

	// Bootstrap a channel directly via the store (no router needed; the
	// publish path is what we're exercising).
	require.NoError(t, srv.channelStore.CreateChannelWithMembers(t.Context(),
		channels.Channel{ID: "group:planning", Name: "planning", Type: channels.ChannelTypeGroup},
		[]channels.Member{{ParticipantID: "alice", RespondPolicy: channels.RespondAlways}}))

	pubBody, _ := json.Marshal(publishMessageRequest{SenderID: "alice", Content: "hi"})
	for i := 0; i < 3; i++ {
		rec := doRequest(srv.Handler(), http.MethodPost,
			"/api/v1/channels/group:planning/messages", pubBody)
		require.Equal(t, http.StatusCreated, rec.Code,
			"publish #%d via fallback must succeed: body=%s", i+1, rec.Body.String())
	}

	warnings := recorded.FilterMessageSnippet("router-nil fallback").All()
	assert.Len(t, warnings, 1,
		"router-nil fallback Warn must fire exactly once across multiple publishes")
}

// TestChannels_PublishMessage_RouterNilFallback_SkipsChannelTypeCheck
// documents the residual gap that the Warn signposts: the fallback
// path does NOT cross-validate channel_type. We pin the current
// behaviour so a future refactor (e.g. moving the cross-check into the
// store) does not silently change it without an explicit test update.
//
// When PR-4 lands the gRPC dispatcher and removes the fallback (the
// router becomes mandatory), this test should be deleted alongside the
// fallback branch.
func TestChannels_PublishMessage_RouterNilFallback_SkipsChannelTypeCheck(t *testing.T) {
	channelFallbackWarnOnce = sync.Once{}
	t.Cleanup(func() { channelFallbackWarnOnce = sync.Once{} })

	srv, _ := channelTestServerNoRouter(t)

	require.NoError(t, srv.channelStore.CreateChannelWithMembers(t.Context(),
		channels.Channel{ID: "group:planning", Name: "planning", Type: channels.ChannelTypeGroup},
		[]channels.Member{{ParticipantID: "alice", RespondPolicy: channels.RespondAlways}}))

	// channel_type=dm against a group channel — the router would 400.
	// The fallback path skips this validation by design (writes directly
	// to the store), so the publish succeeds. The Warn above is the
	// signpost for this trade-off.
	body, _ := json.Marshal(publishMessageRequest{
		SenderID:    "alice",
		Content:     "x",
		ChannelType: "dm",
	})
	rec := doRequest(srv.Handler(), http.MethodPost,
		"/api/v1/channels/group:planning/messages", body)
	assert.Equal(t, http.StatusCreated, rec.Code,
		"fallback path skips channel_type validation by design (Warn signposts)")
}
