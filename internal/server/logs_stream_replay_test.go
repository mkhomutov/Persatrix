// Tests for the buffered entries the SSE log stream sends for `since`
// (ISSUE-0223): which entries they are, how they meet the live ones, and
// that writing them neither blocks the buffer nor escapes the write
// deadline. The filters themselves are in logs_stream_filters_test.go.
package server

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/observability/logbuffer"
	"github.com/mkhomutov/persatrix/internal/planner"
	"github.com/mkhomutov/persatrix/internal/registry"
	"github.com/mkhomutov/persatrix/internal/state"
)

// The buffered entries sent for `since` are the ones the snapshot route
// returns for the same filters with no `limit`: the newest 1,000.
func TestSSE_SinceSendsWhatTheSnapshotRouteReturns(t *testing.T) {
	srv, buf := testServerWithRing(t, defaultLogsLimit+200)
	hs := httptest.NewServer(srv.Handler())
	defer closeServer(t, hs)
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()

	start := time.Now().UTC().Add(-time.Minute)
	for i := range defaultLogsLimit + 50 {
		ts := start.Add(time.Duration(i) * time.Millisecond)
		require.Equal(t, logbuffer.DropNone, buf.Append(mkEntry("exec-1", "WARN", fmt.Sprintf("m%04d", i), ts, nil)))
	}
	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/executions/exec-1/logs?since=5m", nil)
	require.Equal(t, http.StatusOK, rec.Code)
	var snapshot []logbuffer.Entry
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &snapshot))
	require.Len(t, snapshot, defaultLogsLimit)
	require.Equal(t, "m0050", snapshot[0].Message, "the oldest fifty are past the limit")

	stream := openSSE(ctx, t, hs, "/api/v1/executions/exec-1/logs/stream?since=5m")
	buf.Append(mkEntry("exec-1", "WARN", "end", time.Now().UTC(), nil))
	got := stream.messagesUntil("end")
	// Compared one by one: a thousand-entry diff helps nobody.
	require.Len(t, got, len(snapshot)+1)
	for i, e := range snapshot {
		require.Equal(t, e.Message, got[i], "entry %d of the stream", i)
	}
}

// testServerWithRing is testServerWithBuffer with a ring of the given
// size, for the cases that need more than its 100 entries.
func testServerWithRing(t *testing.T, perExecution int) (*Server, *logbuffer.Buffer) {
	t.Helper()
	logger := zap.NewNop()
	buf, err := logbuffer.New(logbuffer.Config{
		Dir:           t.TempDir(),
		PerExecution:  perExecution,
		MaxExecutions: 8,
		DiskCapBytes:  1 << 20,
		DropLevel:     "DEBUG",
		RatePerExec:   1000,
	}, logger)
	require.NoError(t, err)
	t.Cleanup(func() { _ = buf.Close() })
	srv, err := New("127.0.0.1:0", t.TempDir(), state.NewInMemoryStore(logger),
		registry.NewInMemoryRegistry(logger), planner.NewYAMLPlanner(logger), logger, WithLogBuffer(buf))
	require.NoError(t, err)
	return srv, buf
}

// A writer appends while a client connects with `since`. Whatever the
// moment the subscription lands, the client gets each entry once: the
// ones written before it from the buffer, the rest live, with no entry
// missing and none repeated where the two meet. Odd rounds write as fast
// as they can, even rounds pause between entries so that a round can stay
// under the 1,000-entry cap and be checked from its first entry.
func TestSSE_SinceMeetsTheLiveStreamWithNoLossAndNoRepeat(t *testing.T) {
	const (
		rounds        = 24
		ringSize      = 1 << 15
		afterAttached = 20
	)
	for round := range rounds {
		srv, buf := testServerWithRing(t, ringSize)
		hs := httptest.NewServer(srv.Handler())
		ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)

		written := make(chan int, 1)
		var wg sync.WaitGroup
		wg.Add(1)
		go func() {
			defer wg.Done()
			n, left := 0, -1
			for left != 0 && n < ringSize-afterAttached-1 {
				if left < 0 && buf.SubscriberCount() == 1 {
					left = afterAttached
				}
				if buf.Append(mkEntry("exec-1", "WARN", fmt.Sprintf("m%06d", n), time.Now().UTC(), nil)) == logbuffer.DropNone {
					n++
					if left > 0 {
						left--
					}
				}
				if round%2 == 0 {
					time.Sleep(20 * time.Microsecond)
				}
			}
			buf.Append(mkEntry("exec-1", "WARN", "end", time.Now().UTC(), nil))
			written <- n
		}()

		stream := openSSE(ctx, t, hs, "/api/v1/executions/exec-1/logs/stream?since=1h")
		got := stream.messagesUntil("end")
		wg.Wait()
		n := <-written

		require.GreaterOrEqual(t, len(got), afterAttached+1, "round %d", round)
		numbered := got[:len(got)-1]
		first := n - len(numbered)
		for i, msg := range numbered {
			require.Equal(t, fmt.Sprintf("m%06d", first+i), msg,
				"round %d: entry %d of %d is out of sequence (an entry was lost or sent twice)", round, i, len(numbered))
		}
		if n <= defaultLogsLimit {
			require.Zero(t, first, "round %d: %d entries were written and the stream starts at %d", round, n, first)
		}
		cancel()
		closeServer(t, hs)
	}
}

// A client that asks for buffered entries and then stops reading must
// not hold up the buffer: entries are still admitted, and other readers
// still get a snapshot, while its replay waits on the socket.
func TestSSE_AReplayToAStalledClientDoesNotBlockTheBuffer(t *testing.T) {
	srv, buf := testServerWithBuffer(t)
	hs := httptest.NewServer(srv.Handler())
	defer closeServer(t, hs)

	// About 13 MB of buffered entries: more than the socket buffers of
	// both ends hold, so the handler's writes stall.
	big := strings.Repeat("x", 128<<10)
	now := time.Now().UTC()
	for i := range 100 {
		require.Equal(t, logbuffer.DropNone,
			buf.Append(mkEntry("exec-1", "WARN", big, now.Add(time.Duration(i)*time.Millisecond), nil)))
	}

	conn, err := net.Dial("tcp", hs.Listener.Addr().String())
	require.NoError(t, err)
	defer conn.Close()
	_, err = fmt.Fprintf(conn, "GET /api/v1/executions/exec-1/logs/stream?since=1h HTTP/1.1\r\nHost: x\r\n\r\n")
	require.NoError(t, err)
	// Read the status line only, then never read again.
	status, err := bufio.NewReader(io.LimitReader(conn, 64)).ReadString('\n')
	require.NoError(t, err)
	require.Contains(t, status, "200")
	require.Eventually(t, func() bool { return buf.SubscriberCount() == 1 }, 2*time.Second, 5*time.Millisecond)
	time.Sleep(200 * time.Millisecond) // let the replay fill the socket and stall

	done := make(chan struct{})
	go func() {
		defer close(done)
		buf.Append(mkEntry("exec-1", "WARN", "still-admitted", time.Now().UTC(), nil))
		buf.Append(mkEntry("exec-2", "WARN", "another-execution", time.Now().UTC(), nil))
		_ = buf.Snapshot("exec-1")
	}()
	select {
	case <-done:
	case <-time.After(3 * time.Second):
		require.FailNow(t, "the buffer was blocked while a replay waited on a client that does not read")
	}
	snap := buf.Snapshot("exec-1")
	assert.Equal(t, "still-admitted", snap[len(snap)-1].Message)
}

// deadlineRecorder is a ResponseWriter that notes, for every Write,
// whether a write deadline had been set since the Write before it.
type deadlineRecorder struct {
	mu          sync.Mutex
	header      http.Header
	deadlineSet bool
	writes      int
	unguarded   []string // writes made with no deadline set first
	dataFrames  int
	lastTimeout time.Duration
}

func (d *deadlineRecorder) Header() http.Header { return d.header }
func (d *deadlineRecorder) WriteHeader(int)     {}
func (d *deadlineRecorder) Flush()              {}

func (d *deadlineRecorder) SetWriteDeadline(at time.Time) error {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.deadlineSet = true
	d.lastTimeout = time.Until(at)
	return nil
}

func (d *deadlineRecorder) Write(p []byte) (int, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.writes++
	if !d.deadlineSet {
		d.unguarded = append(d.unguarded, string(p))
	}
	d.deadlineSet = false
	if string(p) == "data: " {
		d.dataFrames++
	}
	return len(p), nil
}

// Every write of the buffered entries is made under the same write
// deadline as a live entry's, so a client that stops reading during the
// replay is dropped after the deadline and does not hold its slot.
func TestSSE_TheReplayIsWrittenUnderTheWriteDeadline(t *testing.T) {
	srv, buf := testServerWithBuffer(t)
	now := time.Now().UTC()
	for i := range 5 {
		buf.Append(mkEntry("exec-1", "INFO", fmt.Sprintf("m%d", i), now.Add(time.Duration(i-10)*time.Second), nil))
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/executions/exec-1/logs/stream?since=1h", nil).WithContext(ctx)
	req.SetPathValue("id", "exec-1")
	rec := &deadlineRecorder{header: http.Header{}}
	done := make(chan struct{})
	go func() {
		defer close(done)
		srv.handleStreamLogs(rec, req)
	}()
	require.Eventually(t, func() bool {
		rec.mu.Lock()
		defer rec.mu.Unlock()
		return rec.dataFrames == 5
	}, 3*time.Second, 5*time.Millisecond, "the five buffered entries were not written")
	cancel()
	<-done

	rec.mu.Lock()
	defer rec.mu.Unlock()
	assert.Empty(t, rec.unguarded, "writes made with no write deadline set")
	assert.InDelta(t, sseWriteTimeout.Seconds(), rec.lastTimeout.Seconds(), 5)
}
