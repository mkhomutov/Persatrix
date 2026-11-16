// Tests for which entries the two log routes select and in which order
// (ISSUE-0223): the limit is counted after the filters, the merged view
// has the same limit and one order for entries with one timestamp, a
// stream without `since` sends nothing buffered, and a relative `since`
// is resolved once.
package server

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/mkhomutov/persatrix/internal/observability/logbuffer"
)

// snapshotMessages asks the snapshot route and returns the messages.
func snapshotMessages(t *testing.T, srv *Server, path string) []string {
	t.Helper()
	rec := doRequest(srv.Handler(), http.MethodGet, path, nil)
	require.Equal(t, http.StatusOK, rec.Code)
	var entries []logbuffer.Entry
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &entries))
	out := make([]string, 0, len(entries))
	for _, e := range entries {
		out = append(out, e.Message)
	}
	return out
}

// streamMessages opens the stream route, logs a closing ERROR entry for
// the execution closeOn and returns the messages sent before it.
func streamMessages(t *testing.T, srv *Server, buf *logbuffer.Buffer, path, closeOn string) []string {
	t.Helper()
	hs := httptest.NewServer(srv.Handler())
	defer closeServer(t, hs)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	stream := openSSE(ctx, t, hs, path)
	require.Equal(t, logbuffer.DropNone, buf.Append(mkEntry(closeOn, "ERROR", "end", time.Now().UTC(), nil)))
	got := stream.messagesUntil("end")
	return got[:len(got)-1]
}

// The newest 1,000 are counted among the entries that pass the filters,
// not among all of them: a hundred errors followed by a thousand
// warnings are a hundred errors for `level=ERROR`.
func TestLogs_TheLimitIsCountedAfterTheFilters(t *testing.T) {
	srv, buf := testServerWithRing(t, defaultLogsLimit+200)
	start := time.Now().UTC().Add(-time.Minute)
	var want []string
	for i := range 100 {
		msg := fmt.Sprintf("error-%03d", i)
		require.Equal(t, logbuffer.DropNone,
			buf.Append(mkEntry("exec-1", "ERROR", msg, start.Add(time.Duration(i)*time.Millisecond), nil)))
		want = append(want, msg)
	}
	for i := range defaultLogsLimit {
		require.Equal(t, logbuffer.DropNone,
			buf.Append(mkEntry("exec-1", "WARN", "warning", start.Add(time.Duration(100+i)*time.Millisecond), nil)))
	}
	assert.Equal(t, want, snapshotMessages(t, srv, "/api/v1/executions/exec-1/logs?since=5m&level=ERROR"))
	assert.Equal(t, want, streamMessages(t, srv, buf, "/api/v1/executions/exec-1/logs/stream?since=5m&level=ERROR", "exec-1"))
}

// On the merged view, entries of different executions that carry one
// timestamp come in one order, by execution id and then as each
// execution logged them, on both routes and on every connection. The
// newest 1,000 are cut from that order, so the same entries survive the
// cut every time. The messages begin with the entry's number, so that
// ordering by message would mix the executions.
func TestLogs_EntriesWithOneTimestampComeInOneOrderOnBothRoutes(t *testing.T) {
	const perRun = 150 // eight runs: 1,200 entries, 200 past the limit
	stamp := time.Now().UTC().Add(-time.Minute)
	filled := func() (*Server, *logbuffer.Buffer) {
		// A ring with room for the closing entry as well.
		srv, buf := testServerWithRing(t, perRun+1)
		// Logged in an order that is not the order of the ids.
		for _, id := range []string{"run-e", "run-b", "run-h", "run-a", "run-g", "run-c", "run-f", "run-d"} {
			for i := range perRun {
				require.Equal(t, logbuffer.DropNone,
					buf.Append(mkEntry(id, "WARN", fmt.Sprintf("%03d of %s", i, id), stamp, nil)))
			}
		}
		return srv, buf
	}
	var all []string
	for _, id := range []string{"run-a", "run-b", "run-c", "run-d", "run-e", "run-f", "run-g", "run-h"} {
		for i := range perRun {
			all = append(all, fmt.Sprintf("%03d of %s", i, id))
		}
	}
	want := all[len(all)-defaultLogsLimit:]
	require.Equal(t, "050 of run-b", want[0], "run-a and the start of run-b are past the limit")

	srv, _ := filled()
	got := snapshotMessages(t, srv, "/api/v1/executions/_/logs?since=5m")
	require.Len(t, got, defaultLogsLimit)
	require.Equal(t, want, got, "the snapshot route")
	// A new buffer for each connection: the order in which a buffer hands
	// out its executions differs from one to the next.
	for connection := range 12 {
		srv, buf := filled()
		got := streamMessages(t, srv, buf, "/api/v1/executions/_/logs/stream?since=5m", "run-h")
		require.Len(t, got, defaultLogsLimit, "connection %d: the merged view has the same limit", connection)
		require.Equal(t, want, got, "connection %d of the stream route", connection)
	}
}

// Without `since` a stream sends nothing from the buffer, on one
// execution and on the merged view alike.
func TestSSE_WithoutSinceNothingBufferedIsSent(t *testing.T) {
	for _, id := range []string{"exec-1", "_"} {
		t.Run(id, func(t *testing.T) {
			srv, buf := testServerWithBuffer(t)
			require.Equal(t, logbuffer.DropNone,
				buf.Append(mkEntry("exec-1", "ERROR", "buffered", time.Now().UTC(), nil)))
			assert.Empty(t, streamMessages(t, srv, buf, "/api/v1/executions/"+id+"/logs/stream", "exec-1"))
			assert.Empty(t, streamMessages(t, srv, buf, "/api/v1/executions/"+id+"/logs/stream?level=ERROR", "exec-1"))
		})
	}
}

// A relative `since` is resolved once, when the client connects. It does
// not move forward while the stream is open: an entry stamped inside it
// and delivered late, as an agent's shipper does, is still sent.
func TestSSE_ARelativeSinceIsResolvedOncePerConnection(t *testing.T) {
	const window = 400 * time.Millisecond
	srv, buf := testServerWithBuffer(t)
	hs := httptest.NewServer(srv.Handler())
	defer closeServer(t, hs)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	stream := openSSE(ctx, t, hs, "/api/v1/executions/exec-1/logs/stream?since="+window.String())
	connected := time.Now().UTC()
	time.Sleep(window + 200*time.Millisecond)
	require.Equal(t, logbuffer.DropNone,
		buf.Append(mkEntry("exec-1", "WARN", "stamped-before-the-connection", connected.Add(-window/2), nil)))
	require.Equal(t, logbuffer.DropNone, buf.Append(mkEntry("exec-1", "WARN", "end", time.Now().UTC(), nil)))
	assert.Equal(t, []string{"stamped-before-the-connection", "end"}, stream.messagesUntil("end"))
}
