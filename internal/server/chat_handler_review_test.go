package server

import (
	"context"
	"encoding/json"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
	"go.uber.org/zap/zaptest/observer"

	"github.com/mkhomutov/persatrix/internal/channels"
	"github.com/mkhomutov/persatrix/internal/planner"
	"github.com/mkhomutov/persatrix/internal/registry"
	"github.com/mkhomutov/persatrix/internal/state"
)

// chat_handler_review_test.go — additional chat-handler edge cases
// (concurrent DM, client cancel, DB error vs. validation error) split
// out to keep chat_handler_test.go under the 500-line project limit.
// All fixtures (chatTestServer,
// registerHealthyAgent, doRequest) live in chat_handler_test.go and
// server_helpers_test.go and are visible here within package server.

// TestHandleChat_ConcurrentSameDMReturns409 pins that two concurrent
// chat requests on the same `(user, agent)` DM do not collide on an
// opaque 500. The first request installs a `replyWaiter` for
// `(dm.ID, agentID)` via `PublishAndAwait`; the second request hits
// `ErrWaiterAlreadyRegistered` on its own `Register` call and the
// handler MUST surface that as 409 Conflict (the per-DM serialisation
// is part of the chat-as-DM contract — RFC 0011 amendment §"Single
// in-flight chat per DM"). Pre-fix behaviour: the wrapped error fell
// through the `default:` branch and produced a 500 INTERNAL with no
// actionable signal for the caller.
func TestHandleChat_ConcurrentSameDMReturns409(t *testing.T) {
	srv, reg, _, _ := chatTestServer(t)
	registerHealthyAgent(t, reg, "agent-x", "Agent X")

	// Request 1: never gets a reply, will time out (504). This is the
	// vehicle for keeping a waiter parked on `(dm, agent-x)`.
	body1, _ := json.Marshal(chatRequest{
		Message: "first", UserID: "alice", TimeoutSeconds: 2,
	})
	body2, _ := json.Marshal(chatRequest{
		Message: "second", UserID: "alice", TimeoutSeconds: 1,
	})

	rec1Done := make(chan *httptest.ResponseRecorder, 1)
	go func() {
		rec1Done <- doRequest(srv.Handler(), "POST", "/api/v1/agents/agent-x/chat", body1)
	}()

	// Give request 1 enough time to reach `PublishAndAwait`'s
	// `Register` and park on its waiter chan. The chat path runs
	// validation → registry lookup → SQLite GetOrCreateDM → Register
	// → Publish → select; 100ms is comfortably past that on CI hardware.
	time.Sleep(100 * time.Millisecond)

	rec2 := doRequest(srv.Handler(), "POST", "/api/v1/agents/agent-x/chat", body2)
	assert.Equal(t, 409, rec2.Code, "concurrent chat on same DM must surface as 409 Conflict, not 500")
	assert.Contains(t, rec2.Body.String(), "in flight",
		"409 body must point the caller at the per-DM serialisation contract")

	// Drain request 1 to keep the goroutine from leaking the test scope.
	rec1 := <-rec1Done
	assert.Equal(t, 504, rec1.Code, "request 1 still observes the timeout envelope")
}

// TestHandleChat_ContextCanceledReturnsClientClosed pins that a
// caller disconnect mid-flight does not surface as a 500 INTERNAL
// in the orchestrator error log. `PublishAndAwait` returns
// `ctx.Err()` (typically `context.Canceled`) when the request
// context is cancelled before the reply arrives; pre-fix behaviour
// fell through the handler's `default:` branch and (a) emitted an
// `s.logger.Error` line, (b) returned 500 to a client that was
// already gone. Both are alarm-fatigue and observability noise.
//
// The handler MUST map this to 499 ("Client Closed Request" — the
// nginx-style non-standard code Go has no const for) and log at
// Info, not Error.
func TestHandleChat_ContextCanceledReturnsClientClosed(t *testing.T) {
	srv, reg, _, _ := chatTestServer(t)
	registerHealthyAgent(t, reg, "agent-x", "Agent X")

	ctx, cancel := context.WithCancel(context.Background())
	body, _ := json.Marshal(chatRequest{
		Message: "Hi", UserID: "alice", TimeoutSeconds: 30,
	})
	req := httptest.NewRequest("POST", "/api/v1/agents/agent-x/chat",
		strings.NewReader(string(body))).WithContext(ctx)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	done := make(chan struct{})
	go func() {
		srv.Handler().ServeHTTP(rec, req)
		close(done)
	}()

	// Let the handler reach `PublishAndAwait`'s select on `ctx.Done()`,
	// then cancel mid-flight. No agent reply is ever published.
	time.Sleep(100 * time.Millisecond)
	cancel()

	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("handler did not return after context cancellation")
	}

	assert.Equal(t, 499, rec.Code,
		"client-disconnect mid-flight must map to 499, not 500")
}

// TestHandleChat_GetOrCreateDM_DBErrorReturns500 pins that a non-
// validation failure from `channelStore.GetOrCreateDM` (e.g. a
// transient SQLite I/O error, lock contention, disk-full, or — as
// simulated here — the DB handle being closed underneath the
// handler) surfaces as 500 INTERNAL, NOT 400 BAD_REQUEST.
//
// Pre-fix behaviour: the handler
// mapped EVERY error from `GetOrCreateDM` to
// `400 BAD_REQUEST: invalid participant id`. That conflates two
// disjoint failure classes:
//
//  1. caller-supplied id-hygiene errors (empty / colon / whitespace /
//     same-as-agent), correctly 400 with `ErrInvalidParticipantID`;
//  2. server-side I/O failures (BeginTx / INSERT / Commit), which are
//     5xx — the caller did nothing wrong and the actionable signal is
//     "retry later", not "fix your input".
//
// Surfacing (2) as 400 misleads clients (no retry), poisons error
// budgets (false-low 4xx ratio), and hides genuine outages from
// dashboards built on the 5xx error log line. The fix discriminates
// via `errors.Is(err, channels.ErrInvalidParticipantID)`.
//
// Test mechanism: close the SQLite store after wiring the server but
// before issuing the request. `GetOrCreateDM`'s `BeginTx` then
// returns `sql: database is closed`, which is NOT
// `ErrInvalidParticipantID`, so the handler must take the 5xx
// branch.
func TestHandleChat_GetOrCreateDM_DBErrorReturns500(t *testing.T) {
	srv, reg, _, store := chatTestServer(t)
	registerHealthyAgent(t, reg, "agent-x", "Agent X")

	// Force a non-validation failure path. The chatTestServer cleanup
	// already calls Close, so the second close (no-op for sql.DB) is
	// harmless.
	require.NoError(t, store.Close())

	body, _ := json.Marshal(chatRequest{Message: "Hi", UserID: "alice"})
	rec := doRequest(srv.Handler(), "POST", "/api/v1/agents/agent-x/chat", body)
	assert.Equal(t, 500, rec.Code,
		"non-validation GetOrCreateDM error must surface as 500, not 400")
	assert.Contains(t, rec.Body.String(), "internal server error")
}

// TestHandleChat_GetOrCreateDM_ValidationErrorStillReturns400 pins
// the other half of the discrimination fix: the validation path
// (`ErrInvalidParticipantID` from `CanonicalDMID`) MUST keep its 400
// envelope. Complements `TestHandleChat_InvalidUserIDRejected`
// (which exercises the colon-in-id case) by exercising the
// "user_id == agent_id" rejection so we cover both
// `ErrInvalidParticipantID` formatting paths.
func TestHandleChat_GetOrCreateDM_ValidationErrorStillReturns400(t *testing.T) {
	srv, reg, _, _ := chatTestServer(t)
	registerHealthyAgent(t, reg, "agent-x", "Agent X")

	// user_id == agent_id triggers `ErrInvalidParticipantID` from
	// `CanonicalDMID` ("dm requires two distinct participants").
	body, _ := json.Marshal(chatRequest{Message: "Hi", UserID: "agent-x"})
	rec := doRequest(srv.Handler(), "POST", "/api/v1/agents/agent-x/chat", body)
	assert.Equal(t, 400, rec.Code,
		"validation error from GetOrCreateDM must remain 400")
	assert.Contains(t, rec.Body.String(), "invalid participant id")
}

// chatTestServerWithObserver wires the chat-as-DM stack against a
// zap observer logger so tests can assert log emission counts. Mirrors
// `chatTestServer` but swaps the nop logger for a recording one. The
// observer is filtered to WarnLevel because the only log line under
// test (the `"local"` fallback Warn) is emitted at that level.
func chatTestServerWithObserver(t *testing.T) (*Server, *registry.InMemoryRegistry, *observer.ObservedLogs) {
	t.Helper()
	dbPath := filepath.Join(t.TempDir(), "channels.db")
	store, err := channels.NewSQLiteStore(dbPath, channels.SQLiteOptions{
		MaxChannels: 50,
		Logger:      zap.NewNop(),
	})
	require.NoError(t, err)
	t.Cleanup(func() { _ = store.Close() })
	router := channels.NewChannelRouter(store, channels.NoopDispatcher{}, zap.NewNop(), nil)

	core, recorded := observer.New(zap.WarnLevel)
	logger := zap.New(core)
	wfDir := t.TempDir()
	reg := registry.NewInMemoryRegistry(logger)
	srv, err := New("127.0.0.1:0", wfDir,
		state.NewInMemoryStore(logger),
		reg,
		planner.NewYAMLPlanner(logger),
		logger,
		WithChannels(store, router),
	)
	require.NoError(t, err)
	return srv, reg, recorded
}

// TestHandleChat_LocalFallback_WarnsOncePerProcess pins the
// once-per-process semantics of the `"local"` user_id fallback Warn.
//
// Pre-fix behaviour emitted a Warn on EVERY chat request that omitted
// `user_id`. The cross-talk hazard the Warn signposts is structural
// (single shared `dm:<agent>:local` for all anonymous callers) — it
// does not change between requests. At even modest chat QPS the
// per-request Warn floods the operator's log scrape and drowns out
// the genuinely actionable lines (the structural cross-talk hazard
// is a one-time configuration signal, not a per-request event).
//
// The fix gates the Warn behind a package-level `sync.Once`, matching
// the established `channelFallbackWarnOnce` precedent in
// channel_handlers.go (PR #245 review round 3 Should-Fix #3, also
// hardened by ISSUE-0009 / PR #246 for test-isolation safety).
//
// This test issues three back-to-back chat requests with empty
// `user_id` and asserts the matching Warn appears exactly once in the
// recorded logs. Each request will hit the registry-not-healthy 503
// path (no agent registered) but that is downstream of the fallback
// Warn — the Warn must fire (or, after the second request, must NOT
// fire again) regardless of the eventual handler outcome.
func TestHandleChat_LocalFallback_WarnsOncePerProcess(t *testing.T) {
	TestingResetChatLocalFallbackWarnOnce()
	t.Cleanup(TestingResetChatLocalFallbackWarnOnce)

	srv, reg, recorded := chatTestServerWithObserver(t)
	registerHealthyAgent(t, reg, "agent-x", "Agent X")

	// Use a 1s timeout so each request returns promptly (the noop
	// dispatcher means no agent reply will ever arrive; the request
	// will time out, but the fallback Warn fires long before that on
	// the first call).
	body, _ := json.Marshal(chatRequest{
		Message: "Hi", TimeoutSeconds: 1, // UserID intentionally empty
	})
	for i := 0; i < 3; i++ {
		_ = doRequest(srv.Handler(), "POST", "/api/v1/agents/agent-x/chat", body)
	}

	warnings := recorded.FilterMessageSnippet("'local' fallback").All()
	assert.Len(t, warnings, 1,
		"local fallback Warn must fire exactly once across multiple chat requests; got %d", len(warnings))
}
