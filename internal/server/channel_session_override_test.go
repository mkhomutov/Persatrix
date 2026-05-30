// RFC 0031 Phase 3 PR 4 — `session_id` override on the publish path.
//
// channel_session_handler_test.go pins the Phase-1 boot-default stamp
// (`WithChannelSessionID`). These tests pin the Phase-3 per-request override:
// a `session_id` on the publish body (a) replaces the boot default on the
// persisted row and (b) is threaded onto the dispatch context so the
// downstream `persatrix-session` emission overrides the ISSUE-0082
// auto-binding. The override-vs-auto-binding emission itself is unit-tested in
// internal/channels/grpc_dispatcher_session_test.go; here we assert the
// handler wiring that feeds it.
package server

import (
	"context"
	"encoding/json"
	"net/http"
	"path/filepath"
	"sync"
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

// overrideCapturingDispatcher records the session override observed on the
// dispatch context for every fanout call, so a test can assert the handler
// threaded `channels.WithSessionOverride` down to the dispatch chokepoint.
type overrideCapturingDispatcher struct {
	mu  sync.Mutex
	got []string
}

func (d *overrideCapturingDispatcher) Dispatch(ctx context.Context, _ channels.DispatchEnvelope, _ channels.ChannelMessage) error {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.got = append(d.got, channels.SessionOverrideFromContext(ctx))
	return nil
}

func (d *overrideCapturingDispatcher) overrides() []string {
	d.mu.Lock()
	defer d.mu.Unlock()
	out := make([]string, len(d.got))
	copy(out, d.got)
	return out
}

// publishOverrideTestServer builds a Server whose router fans out through an
// overrideCapturingDispatcher, with a fixed boot-default session so the
// override can be distinguished from the Phase-1 default.
func publishOverrideTestServer(t *testing.T) (*Server, channels.ChannelStore, *overrideCapturingDispatcher) {
	t.Helper()
	dbPath := filepath.Join(t.TempDir(), "channels.db")
	store, err := channels.NewSQLiteStore(dbPath, channels.SQLiteOptions{
		MaxChannels: 50,
		Logger:      zap.NewNop(),
	})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })

	disp := &overrideCapturingDispatcher{}
	router := channels.NewChannelRouter(store, disp, zap.NewNop(), nil)

	logger := zap.NewNop()
	srv, err := New("127.0.0.1:0", t.TempDir(),
		state.NewInMemoryStore(logger),
		registry.NewInMemoryRegistry(logger),
		planner.NewYAMLPlanner(logger),
		logger,
		WithChannels(store, router),
		WithChannelSessionID("boot-default"),
	)
	require.NoError(t, err)
	return srv, store, disp
}

// createPlanningChannel makes a group channel with the sender and one
// recipient agent so fanout dispatches exactly once (to the recipient).
func createPlanningChannel(t *testing.T, h http.Handler) {
	t.Helper()
	body, _ := json.Marshal(createChannelRequest{
		Name: "planning",
		Members: []channelMemberRequest{
			{ID: "alice"},
			{ID: "agent-b", Respond: "always"},
		},
	})
	require.Equal(t, http.StatusCreated,
		doRequest(h, http.MethodPost, "/api/v1/channels", body).Code)
}

// TestPublish_SessionOverride_StampsRowAndThreadsDispatch pins both halves of
// the override: the persisted message row carries the override (not the boot
// default), and the dispatch context the fanout sees carries the same override
// so the `persatrix-session` emission can prefer it over the auto-binding.
func TestPublish_SessionOverride_StampsRowAndThreadsDispatch(t *testing.T) {
	srv, store, disp := publishOverrideTestServer(t)
	createPlanningChannel(t, srv.Handler())

	pubBody, _ := json.Marshal(publishMessageRequest{
		SenderID: "alice", Content: "hello", SessionID: "run-arc-3",
	})
	rec := doRequest(srv.Handler(), http.MethodPost,
		"/api/v1/channels/group:planning/messages", pubBody)
	require.Equal(t, http.StatusCreated, rec.Code, "body=%s", rec.Body.String())

	// (a) persisted row carries the override, not the boot default.
	hist, err := store.GetHistory(context.Background(), "group:planning", 10, time.Time{})
	require.NoError(t, err)
	require.Len(t, hist, 1)
	assert.Equal(t, "run-arc-3", hist[0].SessionID,
		"an explicit session_id must replace the boot-default on the persisted row")

	// (b) the dispatch context carries the override (fanout is detached, so
	// poll until the single recipient dispatch lands).
	require.Eventually(t, func() bool {
		got := disp.overrides()
		return len(got) == 1 && got[0] == "run-arc-3"
	}, 2*time.Second, 10*time.Millisecond,
		"the handler must thread the override onto the dispatch context")
}

// TestPublish_NoSessionOverride_KeepsBootDefault pins the no-regression half:
// without `session_id`, the row carries the boot default and the dispatch
// context carries no override (so the auto-binding stands downstream).
func TestPublish_NoSessionOverride_KeepsBootDefault(t *testing.T) {
	srv, store, disp := publishOverrideTestServer(t)
	createPlanningChannel(t, srv.Handler())

	pubBody, _ := json.Marshal(publishMessageRequest{
		SenderID: "alice", Content: "hello",
	})
	require.Equal(t, http.StatusCreated,
		doRequest(srv.Handler(), http.MethodPost,
			"/api/v1/channels/group:planning/messages", pubBody).Code)

	hist, err := store.GetHistory(context.Background(), "group:planning", 10, time.Time{})
	require.NoError(t, err)
	require.Len(t, hist, 1)
	assert.Equal(t, "boot-default", hist[0].SessionID,
		"absent an override, the Phase-1 boot default still stamps the row")

	require.Eventually(t, func() bool {
		return len(disp.overrides()) == 1
	}, 2*time.Second, 10*time.Millisecond, "dispatch must have fired once")
	assert.Empty(t, disp.overrides()[0],
		"absent an override, no override rides the dispatch context (auto-binding stands)")
}

// TestPublish_InvalidSessionOverride_Rejected pins the fail-loud boundary check
// (PR #469 deep-review finding 1): the override id rides the gRPC
// `persatrix-session` metadata header, which is printable-ASCII only. A control
// or non-ASCII byte would be rejected by the gRPC transport at send time and
// silently fail the fanout dispatch (the publish already returned 201). Reject
// it at the REST boundary with a 400 instead, mirroring how the handler already
// fails loud on a bad sender_id / mention count / cascade depth. The row must
// not be persisted either.
func TestPublish_InvalidSessionOverride_Rejected(t *testing.T) {
	srv, store, _ := publishOverrideTestServer(t)
	createPlanningChannel(t, srv.Handler())

	pubBody, _ := json.Marshal(publishMessageRequest{
		SenderID: "alice", Content: "hello", SessionID: "bad\nid",
	})
	rec := doRequest(srv.Handler(), http.MethodPost,
		"/api/v1/channels/group:planning/messages", pubBody)
	require.Equal(t, http.StatusBadRequest, rec.Code, "body=%s", rec.Body.String())

	var env errorResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &env))
	assert.Equal(t, "BAD_REQUEST", env.Code,
		"a malformed session_id must fail loud with a machine-readable BAD_REQUEST envelope")

	hist, err := store.GetHistory(context.Background(), "group:planning", 10, time.Time{})
	require.NoError(t, err)
	assert.Empty(t, hist, "a rejected publish must not persist a row")
}

// TestSessionOverrideValid pins the charset contract of the override id: it must
// be printable ASCII (0x20–0x7E) so it can ride the gRPC `persatrix-session`
// metadata header. The id is otherwise trusted (not checked against the
// registry), so ad-hoc operator-chosen ids — uppercase, underscores, dots, even
// internal spaces — pass; only wire-illegal control / non-ASCII bytes are
// rejected.
func TestSessionOverrideValid(t *testing.T) {
	cases := []struct {
		name string
		in   string
		want bool
	}{
		{"uuidv7", "0190a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b", true},
		{"reserved legacy", "legacy", true},
		{"label-style id", "run-arc-3", true},
		{"ad-hoc operator id", "My_Session.2024", true},
		{"internal space is legal metadata", "run arc 3", true},
		{"newline", "run\narc", false},
		{"tab", "run\tarc", false},
		{"carriage return", "run\rarc", false},
		{"null byte", "run\x00arc", false},
		{"del", "run\x7farc", false},
		{"non-ascii", "rún-arc", false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			assert.Equal(t, tc.want, sessionOverrideValid(tc.in))
		})
	}
}
