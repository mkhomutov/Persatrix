package security

import (
	"bufio"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// A call that names no agent is reported once per window, not once per call
// (ISSUE-0208).
//
// `rate_limit.unauthenticated_caller` is a security-class audit event, so the
// audit log syncs the file after each one. The limiter used to emit it for
// every call with no agent id, before it used its own allow-or-deny answer,
// so a flood the limiter refused still cost one disk sync per request. It is
// now throttled the way `rate_limit.violated` is, and each line says how many
// calls it stands for.

// anonCalls reads the count an anonymous-caller event carries.
func anonCalls(t *testing.T, ev AuditEvent) int {
	t.Helper()
	n, ok := ev.Detail["calls_since_last_event"].(int)
	require.True(t, ok, "calls_since_last_event must be an int, got %#v", ev.Detail)
	return n
}

func TestRateLimiter_AnonymousCallerEvent_OncePerWindow(t *testing.T) {
	type step struct {
		advance time.Duration // move the clock first
		calls   int           // then make this many calls
		id      string        // as this caller; "" names no agent
	}
	tests := []struct {
		name  string
		limit int
		steps []step
		// wantCounts is one entry per anonymous-caller event, in order: the
		// number of calls that event says it stands for.
		wantCounts   []int
		wantViolated int
	}{
		{
			name: "a burst inside one window writes one line", limit: 60,
			steps:      []step{{calls: 500}},
			wantCounts: []int{1}, wantViolated: 1,
		},
		{
			name: "the next window's first call reports the whole burst", limit: 60,
			steps:      []step{{calls: 500}, {advance: 60 * time.Second, calls: 1}},
			wantCounts: []int{1, 500}, wantViolated: 1,
		},
		{
			name: "a call one second inside the window is not a new line", limit: 60,
			steps:      []step{{calls: 1}, {advance: 59 * time.Second, calls: 1}},
			wantCounts: []int{1},
		},
		{
			name: "a steady caller writes one line a window", limit: 60,
			steps: []step{
				{calls: 1}, {advance: 20 * time.Second, calls: 1}, {advance: 20 * time.Second, calls: 1},
				{advance: 20 * time.Second, calls: 1}, {advance: 20 * time.Second, calls: 1},
				{advance: 40 * time.Second, calls: 1},
			},
			wantCounts: []int{1, 3, 2},
		},
		{
			name: "refused calls are counted like admitted ones", limit: 2,
			steps:      []step{{calls: 5}, {advance: 60 * time.Second, calls: 1}},
			wantCounts: []int{1, 5}, wantViolated: 1,
		},
		{
			name: "a caller that names an agent writes none", limit: 60,
			steps: []step{{calls: 50, id: "agent-a"}},
		},
		{
			name: "a named caller's calls are not counted into the anonymous line", limit: 60,
			steps: []step{
				{calls: 1}, {calls: 10, id: "agent-a"}, {advance: 60 * time.Second, calls: 1},
			},
			wantCounts: []int{1, 1},
		},
		{
			name: "the anonymous line and the violation line do not use each other up", limit: 1,
			steps:      []step{{calls: 2}},
			wantCounts: []int{1}, wantViolated: 1,
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			clk := newFakeClock(time.Unix(1_700_000_000, 0))
			auditor := newRecordingAuditor()
			rl := newTestRateLimiter(t, clk, func(c *RateLimitConfig) {
				c.CallsPerWindow = tt.limit
				c.Auditor = auditor
			})

			for _, s := range tt.steps {
				clk.Advance(s.advance)
				for range s.calls {
					rl.Allow(context.Background(), s.id)
				}
			}

			var counts []int
			for _, e := range auditor.snapshotByType(AuditRateLimitUnauthenticatedCall) {
				counts = append(counts, anonCalls(t, e.event))
				assert.Equal(t, "anonymous", e.event.AgentID)
				assert.Equal(t, 60, e.event.Detail["window_seconds"])
			}
			assert.Equal(t, tt.wantCounts, counts)
			assert.Equal(t, tt.wantViolated, auditor.countByType(AuditRateLimitViolated))
		})
	}
}

// The anonymous line's throttle is the limiter's own, not an entry in the
// per-agent maps. A flood of invented agent ids pushes the anonymous bucket
// out of the tracked set; if that restarted the throttle, each eviction would
// buy the flooder one more synced line.
func TestRateLimiter_AnonymousCallerEvent_SurvivesEvictionAndReset(t *testing.T) {
	tests := []struct {
		name    string
		disturb func(rl *RateLimiter)
	}{
		{name: "the anonymous bucket is evicted", disturb: func(rl *RateLimiter) {
			rl.Allow(context.Background(), "agent-a") // MaxTrackedAgents is 1
		}},
		{name: "the anonymous bucket is reset", disturb: func(rl *RateLimiter) {
			rl.Reset(context.Background(), "", "operator")
		}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			clk := newFakeClock(time.Unix(1_700_000_000, 0))
			auditor := newRecordingAuditor()
			rl := newTestRateLimiter(t, clk, func(c *RateLimitConfig) {
				c.MaxTrackedAgents = 1
				c.Auditor = auditor
			})
			rl.Allow(context.Background(), "")

			tt.disturb(rl)
			rl.Allow(context.Background(), "")

			assert.Equal(t, 1, auditor.countByType(AuditRateLimitUnauthenticatedCall))
		})
	}
}

// The issue's own measurement: 500 anonymous GETs against 60 calls a minute,
// written to a real audit file. 60 are served and 440 refused, and the file
// used to gain 500 synced lines.
func TestRESTMiddleware_ARefusedFloodWritesOneAnonymousLine(t *testing.T) {
	path := filepath.Join(t.TempDir(), "audit.jsonl")
	audit, err := NewFileAuditLogger(path)
	require.NoError(t, err)
	clk := newFakeClock(time.Unix(1_700_000_000, 0))
	rl := newTestRateLimiter(t, clk, func(c *RateLimitConfig) { c.Auditor = audit })
	handler := RESTRateLimitMiddleware(rl, nil)(http.HandlerFunc(
		func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusOK) }))

	served, refused := 0, 0
	for range 500 {
		rec := httptest.NewRecorder()
		handler.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/api/v1/agents", nil))
		switch rec.Code {
		case http.StatusOK:
			served++
		case http.StatusTooManyRequests:
			refused++
		}
	}
	require.NoError(t, audit.Close())

	assert.Equal(t, 60, served)
	assert.Equal(t, 440, refused)
	lines := map[string]int{}
	f, err := os.Open(path)
	require.NoError(t, err)
	defer func() { _ = f.Close() }()
	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		var ev struct {
			EventType string `json:"event_type"`
		}
		require.NoError(t, json.Unmarshal(scanner.Bytes(), &ev))
		lines[ev.EventType]++
	}
	require.NoError(t, scanner.Err())
	assert.Equal(t, 1, lines[string(AuditRateLimitUnauthenticatedCall)])
	assert.Equal(t, 1, lines[string(AuditRateLimitViolated)])
}
