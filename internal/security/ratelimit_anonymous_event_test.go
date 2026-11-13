package security

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
	"go.uber.org/zap/zapcore"
	"go.uber.org/zap/zaptest/observer"
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

// anonSince reads the seconds an anonymous-caller event says have passed
// since the one before it, or -1 when it carries no such field.
func anonSince(ev AuditEvent) int {
	n, ok := ev.Detail["seconds_since_last_event"].(int)
	if !ok {
		return -1
	}
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
		// window is the limiter's window in seconds; 0 means the 60 every
		// other case uses.
		window int
		steps  []step
		// wantCounts is one entry per anonymous-caller event, in order: the
		// number of calls that event says it stands for.
		wantCounts []int
		// wantSince is the seconds each event says have passed since the
		// one before it; -1 for an event that carries no such field.
		wantSince    []int
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
		{
			name: "the limiter's own window sets the pace, not sixty seconds", limit: 60, window: 10,
			steps: []step{
				{calls: 1}, {advance: 9 * time.Second, calls: 1}, {advance: time.Second, calls: 1},
				{advance: 10 * time.Second, calls: 1},
			},
			wantCounts: []int{1, 2, 1}, wantSince: []int{-1, 10, 10},
		},
		{
			name: "a line says how long its count covers", limit: 60,
			steps: []step{
				{calls: 1}, {advance: 30 * time.Second, calls: 30}, {advance: 2 * time.Hour, calls: 1},
			},
			wantCounts: []int{1, 31}, wantSince: []int{-1, 7230},
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			clk := newFakeClock(time.Unix(1_700_000_000, 0))
			auditor := newRecordingAuditor()
			window := 60
			if tt.window != 0 {
				window = tt.window
			}
			rl := newTestRateLimiter(t, clk, func(c *RateLimitConfig) {
				c.CallsPerWindow = tt.limit
				c.WindowSeconds = window
				c.Auditor = auditor
			})

			for _, s := range tt.steps {
				clk.Advance(s.advance)
				for range s.calls {
					rl.Allow(context.Background(), s.id)
				}
			}

			var counts, since []int
			for _, e := range auditor.snapshotByType(AuditRateLimitUnauthenticatedCall) {
				counts = append(counts, anonCalls(t, e.event))
				since = append(since, anonSince(e.event))
				assert.Equal(t, "anonymous", e.event.AgentID)
				assert.Equal(t, window, e.event.Detail["window_seconds"])
			}
			assert.Equal(t, tt.wantCounts, counts)
			if tt.wantSince != nil {
				assert.Equal(t, tt.wantSince, since)
			}
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
			for range 4 { // one line, and three calls counted towards the next
				rl.Allow(context.Background(), "")
			}

			tt.disturb(rl)
			rl.Allow(context.Background(), "")

			assert.Equal(t, 1, auditor.countByType(AuditRateLimitUnauthenticatedCall),
				"the disturbance did not restart the throttle")

			// Nor did it lose the calls counted so far: the next window's
			// line stands for the three before it, the one after it, and
			// its own.
			clk.Advance(60 * time.Second)
			rl.Allow(context.Background(), "")
			lines := auditor.snapshotByType(AuditRateLimitUnauthenticatedCall)
			require.Len(t, lines, 2)
			assert.Equal(t, 5, anonCalls(t, lines[1].event))
		})
	}
}

// refusingAuditor refuses the emits whose turn is listed in fail (the first
// emit of the watched type is turn 1) and records the rest.
type refusingAuditor struct {
	*recordingAuditor
	watch AuditEventType
	fail  map[int]bool
	turn  int
}

func (f *refusingAuditor) Emit(ctx context.Context, ev AuditEvent) error {
	if ev.EventType == f.watch {
		f.turn++
		if f.fail[f.turn] {
			return errors.New("audit: disk full")
		}
	}
	return f.recordingAuditor.Emit(ctx, ev)
}

// A line that cannot be written must not take its calls with it. The count
// is kept for the next line, which is tried a window later, not on the next
// call: a log that cannot be written is no reason to try once a request.
func TestRateLimiter_AnonymousCallerEvent_AFailedWriteKeepsItsCount(t *testing.T) {
	clk := newFakeClock(time.Unix(1_700_000_000, 0))
	auditor := &refusingAuditor{
		recordingAuditor: newRecordingAuditor(),
		watch:            AuditRateLimitUnauthenticatedCall,
		fail:             map[int]bool{2: true},
	}
	core, logs := observer.New(zapcore.WarnLevel)
	rl := newTestRateLimiter(t, clk, func(c *RateLimitConfig) {
		c.Auditor = auditor
		c.Logger = zap.New(core)
	})
	ctx := context.Background()
	made := 0
	call := func(n int) {
		for range n {
			rl.Allow(ctx, "")
			made++
		}
	}

	call(500) // line 1 stands for the first call
	clk.Advance(60 * time.Second)
	call(4) // the write of the next line fails on the first of these
	require.Equal(t, 2, auditor.turn, "one attempt in this window, not one a call")
	clk.Advance(60 * time.Second)
	call(1)

	var counts, since []int
	sum := 0
	for _, e := range auditor.snapshotByType(AuditRateLimitUnauthenticatedCall) {
		counts = append(counts, anonCalls(t, e.event))
		since = append(since, anonSince(e.event))
		sum += anonCalls(t, e.event)
	}
	assert.Equal(t, []int{1, 504}, counts)
	assert.Equal(t, []int{-1, 120}, since, "the count runs from the last line that was written, not the failed attempt")
	assert.Equal(t, made, sum, "the lines add up to the calls made")
	warnings := logs.FilterMessage("rate limiter could not write the unauthenticated-caller audit line; its count is kept for the next one").All()
	require.Len(t, warnings, 1)
	assert.Equal(t, int64(500), warnings[0].ContextMap()["calls_since_last_event"])
}

// The calls counted since the last line would be lost when the orchestrator
// stops. FlushUnauthenticatedCount writes them as one last line.
func TestRateLimiter_FlushUnauthenticatedCount(t *testing.T) {
	tests := []struct {
		name      string
		calls     int
		wantLines []int
	}{
		{name: "calls counted since the last line are written", calls: 500, wantLines: []int{1, 499}},
		{name: "nothing is pending after the only call's own line", calls: 1, wantLines: []int{1}},
		{name: "a limiter that saw no such call writes nothing", calls: 0, wantLines: nil},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			clk := newFakeClock(time.Unix(1_700_000_000, 0))
			auditor := newRecordingAuditor()
			rl := newTestRateLimiter(t, clk, func(c *RateLimitConfig) { c.Auditor = auditor })
			for range tt.calls {
				rl.Allow(context.Background(), "")
			}
			clk.Advance(20 * time.Second)

			rl.FlushUnauthenticatedCount(context.Background())
			rl.FlushUnauthenticatedCount(context.Background()) // a second flush has nothing left

			var counts []int
			lines := auditor.snapshotByType(AuditRateLimitUnauthenticatedCall)
			for _, e := range lines {
				counts = append(counts, anonCalls(t, e.event))
			}
			assert.Equal(t, tt.wantLines, counts)
			if len(lines) == 2 {
				assert.Equal(t, 20, anonSince(lines[1].event))
				assert.Equal(t, "anonymous", lines[1].event.AgentID)
				assert.Equal(t, 60, lines[1].event.Detail["window_seconds"])
			}
		})
	}
}

func TestRateLimiter_FlushUnauthenticatedCount_OnNoLimiter(t *testing.T) {
	// The orchestrator runs with no limiter when rate limiting is turned
	// off; its deferred flush must be safe to call all the same.
	var rl *RateLimiter
	assert.NotPanics(t, func() { rl.FlushUnauthenticatedCount(context.Background()) })
}

// A flush that cannot write keeps the count, like any other line.
func TestRateLimiter_FlushUnauthenticatedCount_AFailedWriteKeepsItsCount(t *testing.T) {
	clk := newFakeClock(time.Unix(1_700_000_000, 0))
	auditor := &refusingAuditor{
		recordingAuditor: newRecordingAuditor(),
		watch:            AuditRateLimitUnauthenticatedCall,
		fail:             map[int]bool{2: true},
	}
	rl := newTestRateLimiter(t, clk, func(c *RateLimitConfig) { c.Auditor = auditor })
	for range 10 {
		rl.Allow(context.Background(), "")
	}

	rl.FlushUnauthenticatedCount(context.Background()) // fails
	rl.FlushUnauthenticatedCount(context.Background())

	lines := auditor.snapshotByType(AuditRateLimitUnauthenticatedCall)
	require.Len(t, lines, 2)
	assert.Equal(t, 9, anonCalls(t, lines[1].event))
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
