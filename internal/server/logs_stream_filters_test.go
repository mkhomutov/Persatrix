// Tests for the filters on the SSE log stream (ISSUE-0223): the stream
// route reads `since`, `workflow` and `level` by the snapshot route's
// rules, applies them to every entry it sends and, with `since`, sends
// the matching buffered entries before the live ones.
package server

import (
	"bufio"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/mkhomutov/persatrix/internal/observability/logbuffer"
)

// sseStream is one open stream response, read frame by frame.
type sseStream struct {
	t      *testing.T
	reader *bufio.Reader
}

// openSSE connects to path on hs and returns once the response headers
// are in. The handler sends them only after it has subscribed, so an
// entry appended after openSSE returns is a live one.
func openSSE(ctx context.Context, t *testing.T, hs *httptest.Server, path string) *sseStream {
	t.Helper()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, hs.URL+path, nil)
	require.NoError(t, err)
	resp, err := http.DefaultClient.Do(req)
	require.NoError(t, err)
	t.Cleanup(func() { _ = resp.Body.Close() })
	require.Equal(t, http.StatusOK, resp.StatusCode)
	return &sseStream{t: t, reader: bufio.NewReader(resp.Body)}
}

// messagesUntil reads data frames until one carries the message last and
// returns every message read, last included. A stream that ends or runs
// into the test's deadline first fails the test with what had arrived.
func (s *sseStream) messagesUntil(last string) []string {
	s.t.Helper()
	var got []string
	for {
		line, err := s.reader.ReadString('\n')
		if err != nil {
			require.FailNow(s.t, "stream ended before the last entry",
				"waiting for %q, got %v (%v)", last, got, err)
		}
		payload, isData := strings.CutPrefix(line, "data: ")
		if !isData {
			continue
		}
		var e logbuffer.Entry
		require.NoError(s.t, json.Unmarshal([]byte(payload), &e))
		got = append(got, e.Message)
		if e.Message == last {
			return got
		}
	}
}

// logLine is one entry of a filter case: its level, message, how long
// ago it is stamped and the workflow attribute it carries, if any.
type logLine struct {
	level    string
	msg      string
	age      time.Duration
	workflow string
}

func (l logLine) entry(now time.Time) logbuffer.Entry {
	var attrs map[string]any
	if l.workflow != "" {
		attrs = map[string]any{"workflow": l.workflow}
	}
	return mkEntry("exec-1", l.level, l.msg, now.Add(-l.age), attrs)
}

func TestSSE_AppliesTheSnapshotRouteFilters(t *testing.T) {
	const minute = time.Minute
	// Three entries in the buffer before the client connects: one older
	// than five minutes and two inside them.
	buffered := []logLine{
		{level: "INFO", msg: "old", age: 10 * minute, workflow: "code-review"},
		{level: "INFO", msg: "recent-info", age: 2 * minute, workflow: "code-review"},
		{level: "ERROR", msg: "recent-error", age: 1 * minute, workflow: "deploy"},
	}
	cases := []struct {
		name  string
		query func(now time.Time) string
		live  []logLine // appended once the client is subscribed; the last must match
		want  []string
	}{
		{
			name:  "no filter: every live entry and nothing from the buffer",
			query: func(time.Time) string { return "" },
			live: []logLine{
				{level: "DEBUG", msg: "d"}, {level: "INFO", msg: "i"},
				{level: "WARN", msg: "w"}, {level: "ERROR", msg: "end"},
			},
			want: []string{"d", "i", "w", "end"},
		},
		{
			name:  "level keeps that level only",
			query: func(time.Time) string { return "?level=INFO" },
			live: []logLine{
				{level: "DEBUG", msg: "d"}, {level: "INFO", msg: "i"},
				{level: "WARN", msg: "w"}, {level: "ERROR", msg: "e"},
				{level: "INFO", msg: "end"},
			},
			want: []string{"i", "end"},
		},
		{
			name:  "level is read in any letter case",
			query: func(time.Time) string { return "?level=warn" },
			live: []logLine{
				{level: "INFO", msg: "i"}, {level: "WARN", msg: "w"},
				{level: "WARN", msg: "end"},
			},
			want: []string{"w", "end"},
		},
		{
			name:  "workflow keeps that workflow only",
			query: func(time.Time) string { return "?workflow=code-review" },
			live: []logLine{
				{level: "INFO", msg: "other", workflow: "deploy"},
				{level: "INFO", msg: "none"},
				{level: "INFO", msg: "mine", workflow: "code-review"},
				{level: "INFO", msg: "end", workflow: "code-review"},
			},
			want: []string{"mine", "end"},
		},
		{
			name:  "since as a duration: buffered entries inside it first, then live ones",
			query: func(time.Time) string { return "?since=5m" },
			live:  []logLine{{level: "INFO", msg: "live"}, {level: "INFO", msg: "end"}},
			want:  []string{"recent-info", "recent-error", "live", "end"},
		},
		{
			name: "since as a timestamp",
			query: func(now time.Time) string {
				return "?since=" + url.QueryEscape(now.Add(-90*time.Second).Format(time.RFC3339))
			},
			live: []logLine{{level: "INFO", msg: "end"}},
			want: []string{"recent-error", "end"},
		},
		{
			name:  "since holds for live entries too: one stamped earlier is not sent",
			query: func(time.Time) string { return "?since=30s" },
			live: []logLine{
				{level: "INFO", msg: "late-arrival", age: 10 * minute},
				{level: "INFO", msg: "end"},
			},
			want: []string{"end"},
		},
		{
			name:  "since with level: the buffered entries are filtered as well",
			query: func(time.Time) string { return "?since=5m&level=ERROR" },
			live:  []logLine{{level: "INFO", msg: "i"}, {level: "ERROR", msg: "end"}},
			want:  []string{"recent-error", "end"},
		},
		{
			name:  "all three together",
			query: func(time.Time) string { return "?since=15m&workflow=code-review&level=INFO" },
			live: []logLine{
				{level: "INFO", msg: "other", workflow: "deploy"},
				{level: "WARN", msg: "warn", workflow: "code-review"},
				{level: "INFO", msg: "end", workflow: "code-review"},
			},
			want: []string{"old", "recent-info", "end"},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			srv, buf := testServerWithBuffer(t)
			hs := httptest.NewServer(srv.Handler())
			defer hs.Close()
			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()

			now := time.Now().UTC()
			for _, l := range buffered {
				require.Equal(t, logbuffer.DropNone, buf.Append(l.entry(now)))
			}
			stream := openSSE(ctx, t, hs, "/api/v1/executions/exec-1/logs/stream"+tc.query(now))
			require.Equal(t, 1, buf.SubscriberCount())
			for _, l := range tc.live {
				require.Equal(t, logbuffer.DropNone, buf.Append(l.entry(now)))
			}
			assert.Equal(t, tc.want, stream.messagesUntil("end"))
		})
	}
}

// With `since` the merged view (`_`) sends the buffered entries of every
// execution, oldest first across them, and then the live ones.
func TestSSE_SinceOnTheMergedViewCoversEveryExecution(t *testing.T) {
	srv, buf := testServerWithBuffer(t)
	hs := httptest.NewServer(srv.Handler())
	defer hs.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	now := time.Now().UTC()
	buf.Append(mkEntry("exec-b", "INFO", "b-second", now.Add(-1*time.Minute), nil))
	buf.Append(mkEntry("exec-a", "INFO", "a-first", now.Add(-2*time.Minute), nil))
	buf.Append(mkEntry("exec-a", "INFO", "a-too-old", now.Add(-20*time.Minute), nil))
	buf.Append(mkEntry("exec-c", "INFO", "c-third", now.Add(-30*time.Second), nil))

	stream := openSSE(ctx, t, hs, "/api/v1/executions/_/logs/stream?since=5m")
	buf.Append(mkEntry("exec-b", "INFO", "end", now, nil))
	assert.Equal(t, []string{"a-first", "b-second", "c-third", "end"}, stream.messagesUntil("end"))
}

// A value the snapshot route refuses is refused by the stream route with
// the same status and the same body, and takes no subscriber slot.
func TestSSE_RefusesTheValuesTheSnapshotRouteRefuses(t *testing.T) {
	cases := []struct {
		name  string
		query string
	}{
		{"a level outside the four", "?level=TRACE"},
		{"a negative duration", "?since=-5m"},
		{"neither a duration nor a timestamp", "?since=yesterday"},
		{"a timestamp in the future", "?since=2999-01-01T00:00:00Z"},
		{"a bad filter beside a good one", "?level=INFO&since=5x"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			srv, buf := testServerWithBuffer(t)
			hs := httptest.NewServer(srv.Handler())
			defer hs.Close()

			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			get := func(path string) (int, string) {
				req, err := http.NewRequestWithContext(ctx, http.MethodGet, hs.URL+path, nil)
				require.NoError(t, err)
				resp, err := http.DefaultClient.Do(req)
				require.NoError(t, err)
				defer resp.Body.Close()
				if resp.StatusCode == http.StatusOK {
					// An open stream never ends by itself; do not read it.
					return resp.StatusCode, ""
				}
				body, err := io.ReadAll(resp.Body)
				require.NoError(t, err)
				return resp.StatusCode, string(body)
			}
			snapStatus, snapBody := get("/api/v1/executions/exec-1/logs" + tc.query)
			require.Equal(t, http.StatusBadRequest, snapStatus, "the snapshot route is the reference")

			status, body := get("/api/v1/executions/exec-1/logs/stream" + tc.query)
			assert.Equal(t, snapStatus, status)
			assert.JSONEq(t, snapBody, body)
			assert.Zero(t, buf.SubscriberCount(), "a refused request must not hold a subscriber slot")
		})
	}
}

// The stream route still does not read `limit`: a value the snapshot
// route refuses does not refuse a stream, as before this change.
func TestSSE_LimitIsStillNotRead(t *testing.T) {
	srv, buf := testServerWithBuffer(t)
	hs := httptest.NewServer(srv.Handler())
	defer hs.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	stream := openSSE(ctx, t, hs, "/api/v1/executions/exec-1/logs/stream?limit=not-a-number")
	buf.Append(mkEntry("exec-1", "INFO", "end", time.Now().UTC(), nil))
	assert.Equal(t, []string{"end"}, stream.messagesUntil("end"))
}
