// ISSUE-0085 PR 5 — `epoch_id` override on the dispatch-bearing REST verbs.
//
// channel_session_override_test.go pins the sibling `--session` override; these
// pin `--epoch`: an `epoch_id` on the publish body (a) is threaded onto the
// dispatch context so the downstream `persatrix-epoch` emission overrides the
// boot epoch (PR 4's WithEpoch), and (b) is rejected at the REST boundary when
// wire-illegal. The override-vs-boot emission itself is unit-tested in
// internal/channels/grpc_dispatcher_epoch_override_test.go; here we assert the
// handler wiring that feeds it. Unlike the session override the epoch is NOT
// stamped on the persisted row (the channel-store column keeps its "live"
// default; run-isolation is enforced persona-side via the gRPC rail), so there
// is no row-stamp assertion — only the dispatch-context threading.
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

// epochOverrideCapturingDispatcher records the epoch override observed on the
// dispatch context for every fanout call, so a test can assert the handler
// threaded channels.WithEpochOverride down to the dispatch chokepoint.
type epochOverrideCapturingDispatcher struct {
	mu  sync.Mutex
	got []string
}

func (d *epochOverrideCapturingDispatcher) Dispatch(ctx context.Context, _ channels.DispatchEnvelope, _ channels.ChannelMessage) error {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.got = append(d.got, channels.EpochOverrideFromContext(ctx))
	return nil
}

func (d *epochOverrideCapturingDispatcher) overrides() []string {
	d.mu.Lock()
	defer d.mu.Unlock()
	out := make([]string, len(d.got))
	copy(out, d.got)
	return out
}

// publishEpochOverrideTestServer builds a Server whose router fans out through
// an epochOverrideCapturingDispatcher. No boot epoch is wired (the override
// path the test exercises does not depend on it — the dispatcher reads the
// context directly).
func publishEpochOverrideTestServer(t *testing.T) (*Server, channels.ChannelStore, *epochOverrideCapturingDispatcher) {
	t.Helper()
	dbPath := filepath.Join(t.TempDir(), "channels.db")
	store, err := channels.NewSQLiteStore(dbPath, channels.SQLiteOptions{
		MaxChannels: 50,
		Logger:      zap.NewNop(),
	})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })

	disp := &epochOverrideCapturingDispatcher{}
	router := channels.NewChannelRouter(store, disp, zap.NewNop(), nil)

	logger := zap.NewNop()
	srv, err := New("127.0.0.1:0", t.TempDir(),
		state.NewInMemoryStore(logger),
		registry.NewInMemoryRegistry(logger),
		planner.NewYAMLPlanner(logger),
		logger,
		WithChannels(store, router),
	)
	require.NoError(t, err)
	return srv, store, disp
}

// TestPublish_EpochOverride_ThreadsDispatch pins that an explicit `epoch_id` on
// the publish body is threaded onto the dispatch context so the downstream
// `persatrix-epoch` emission can prefer it over the boot epoch.
func TestPublish_EpochOverride_ThreadsDispatch(t *testing.T) {
	srv, _, disp := publishEpochOverrideTestServer(t)
	createPlanningChannel(t, srv.Handler())

	pubBody, _ := json.Marshal(publishMessageRequest{
		SenderID: "alice", Content: "hello", EpochID: "ci-run-5",
	})
	rec := doRequest(srv.Handler(), http.MethodPost,
		"/api/v1/channels/group:planning/messages", pubBody)
	require.Equal(t, http.StatusCreated, rec.Code, "body=%s", rec.Body.String())

	require.Eventually(t, func() bool {
		got := disp.overrides()
		return len(got) == 1 && got[0] == "ci-run-5"
	}, 2*time.Second, 10*time.Millisecond,
		"the handler must thread the epoch override onto the dispatch context")
}

// TestPublish_NoEpochOverride_NoOverrideOnDispatch pins the no-regression half:
// without `epoch_id`, no override rides the dispatch context (the boot epoch
// stands downstream).
func TestPublish_NoEpochOverride_NoOverrideOnDispatch(t *testing.T) {
	srv, _, disp := publishEpochOverrideTestServer(t)
	createPlanningChannel(t, srv.Handler())

	pubBody, _ := json.Marshal(publishMessageRequest{
		SenderID: "alice", Content: "hello",
	})
	require.Equal(t, http.StatusCreated,
		doRequest(srv.Handler(), http.MethodPost,
			"/api/v1/channels/group:planning/messages", pubBody).Code)

	require.Eventually(t, func() bool {
		return len(disp.overrides()) == 1
	}, 2*time.Second, 10*time.Millisecond, "dispatch must have fired once")
	assert.Empty(t, disp.overrides()[0],
		"absent an override, no epoch override rides the dispatch context (boot epoch stands)")
}

// TestPublish_InvalidEpochOverride_Rejected pins the fail-loud boundary check:
// the override id rides the gRPC `persatrix-epoch` metadata header, which is
// printable-ASCII only. A control / non-ASCII byte is rejected at the REST
// boundary with a 400 (mirroring the session override), and the row is not
// persisted.
func TestPublish_InvalidEpochOverride_Rejected(t *testing.T) {
	srv, store, _ := publishEpochOverrideTestServer(t)
	createPlanningChannel(t, srv.Handler())

	pubBody, _ := json.Marshal(publishMessageRequest{
		SenderID: "alice", Content: "hello", EpochID: "bad\nepoch",
	})
	rec := doRequest(srv.Handler(), http.MethodPost,
		"/api/v1/channels/group:planning/messages", pubBody)
	require.Equal(t, http.StatusBadRequest, rec.Code, "body=%s", rec.Body.String())

	var env errorResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &env))
	assert.Equal(t, "BAD_REQUEST", env.Code,
		"a malformed epoch_id must fail loud with a machine-readable BAD_REQUEST envelope")

	hist, err := store.GetHistory(context.Background(), "group:planning", 10, time.Time{})
	require.NoError(t, err)
	assert.Empty(t, hist, "a rejected publish must not persist a row")
}

// TestResolveEpochOverride pins the shared helper both handlers call: a blank /
// whitespace-only value is absent (ctx unchanged, no error); a valid id threads
// the override onto ctx; a wire-illegal id is rejected with an error the caller
// surfaces as a 400.
func TestResolveEpochOverride(t *testing.T) {
	s := &Server{}

	t.Run("blank is absent", func(t *testing.T) {
		ctx, err := s.resolveEpochOverride(context.Background(), "   ")
		require.NoError(t, err)
		assert.Empty(t, channels.EpochOverrideFromContext(ctx),
			"a blank epoch_id must leave the context override-free (boot epoch stands)")
	})
	t.Run("valid threads override", func(t *testing.T) {
		ctx, err := s.resolveEpochOverride(context.Background(), "ci-run-5")
		require.NoError(t, err)
		assert.Equal(t, "ci-run-5", channels.EpochOverrideFromContext(ctx))
	})
	t.Run("wire-illegal rejected", func(t *testing.T) {
		_, err := s.resolveEpochOverride(context.Background(), "bad\nepoch")
		require.Error(t, err,
			"a control byte must be rejected (it would fail the gRPC persatrix-epoch send)")
	})
}
