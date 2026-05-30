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
