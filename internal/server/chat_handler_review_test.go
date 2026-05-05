package server

import (
	"context"
	"encoding/json"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
)

// chat_handler_review_test.go — findings from the PR #251 deep review
// that require their own test file to keep chat_handler_test.go under
// the 500-line project limit.  All fixtures (chatTestServer,
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
// actionable signal for the caller. PR #251 review finding
// "Should fix #2".
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
// Info, not Error. PR #251 review finding "Should fix #1".
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
