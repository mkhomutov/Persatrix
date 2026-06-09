package server

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/channels"
	"github.com/mkhomutov/persatrix/internal/planner"
	"github.com/mkhomutov/persatrix/internal/registry"
	"github.com/mkhomutov/persatrix/internal/state"
)

// blockingDispatcher blocks every Dispatch on `release` so a test can prove the
// REST publish handler returns WITHOUT waiting for fanout: while a recipient
// dispatch is parked here, the POST must already have answered 201.
type blockingDispatcher struct {
	started chan struct{} // buffered; one token per Dispatch entry
	release chan struct{} // closed by the test to unblock all dispatches

	mu    sync.Mutex
	calls int
}

func (d *blockingDispatcher) Dispatch(_ context.Context, _ channels.DispatchEnvelope, _ channels.ChannelMessage) error {
	d.mu.Lock()
	d.calls++
	d.mu.Unlock()
	select {
	case d.started <- struct{}{}:
	default:
	}
	<-d.release
	return nil
}

func (d *blockingDispatcher) callCount() int {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.calls
}

// asyncPublishTestServer wires a server whose router uses a caller-supplied
// dispatcher, returning the router too so the test can drain async fanout.
func asyncPublishTestServer(t *testing.T, disp channels.MessageDispatcher) (*Server, *channels.ChannelRouter) {
	t.Helper()
	dbPath := filepath.Join(t.TempDir(), "channels.db")
	store, err := channels.NewSQLiteStore(dbPath, channels.SQLiteOptions{
		MaxChannels: 50,
		Logger:      zap.NewNop(),
	})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })

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
	return srv, router
}

// TestHandlePublishMessage_ReturnsBeforeSlowFanout pins the RFC 0048 console
// publish-latency fix at the HTTP boundary: a `POST .../messages` must answer
// 201 as soon as the message is persisted, NOT after the agent fanout it
// triggers. The synchronous `ChannelRouter.Publish` blocked the response on
// fanout (with floor control on, a serialized multi-turn LLM round up to 45s
// per speaker), so the console composer sat disabled with no feedback for
// 90-135s. `PublishAsync` detaches the fanout.
//
// A single non-sender responder keeps fanout on the concurrent path (floor
// control needs ≥2 responders), and the blockingDispatcher parks that one
// dispatch so a REGRESSION to synchronous publish would hang the POST until
// the test's deadline fires — a loud, deterministic failure.
func TestHandlePublishMessage_ReturnsBeforeSlowFanout(t *testing.T) {
	disp := &blockingDispatcher{
		started: make(chan struct{}, 4),
		release: make(chan struct{}),
	}
	srv, router := asyncPublishTestServer(t, disp)
	handler := srv.Handler()

	// alice (sender) + bob (always) — bob is the lone responder, so fanout
	// takes the concurrent path and parks on the one blocked dispatch.
	createBody, _ := json.Marshal(createChannelRequest{
		Name: "planning",
		Members: []channelMemberRequest{
			{ID: "alice", Respond: "always"},
			{ID: "bob", Respond: "always"},
		},
	})
	require.Equal(t, http.StatusCreated,
		doRequest(handler, http.MethodPost, "/api/v1/channels", createBody).Code)

	pubBody, _ := json.Marshal(map[string]any{
		"sender_id": "alice",
		"content":   "hello team",
	})

	done := make(chan *httptest.ResponseRecorder, 1)
	go func() {
		done <- doRequest(handler, http.MethodPost,
			"/api/v1/channels/group:planning/messages", pubBody)
	}()

	select {
	case rec := <-done:
		require.Equal(t, http.StatusCreated, rec.Code, "body=%s", rec.Body.String())
	case <-time.After(3 * time.Second):
		t.Fatal("POST blocked on fanout: publish handler did not return at the persistence boundary")
	}

	// Fanout is genuinely running, just detached — its dispatch is parked here.
	select {
	case <-disp.started:
	case <-time.After(3 * time.Second):
		t.Fatal("detached fanout never dispatched to the recipient")
	}

	// Unblock and drain so the goroutine cannot outlive the test.
	close(disp.release)
	router.WaitForPendingFanout()
	require.Equal(t, 1, disp.callCount(), "the lone non-sender recipient must be dispatched exactly once")
}
