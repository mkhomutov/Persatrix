// A client that reads when the test says so, for the log stream tests
// that need a reader who is behind (logs_stream_seam_test.go).
package server

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
	"go.uber.org/zap/zaptest/observer"

	"github.com/mkhomutov/persatrix/internal/observability/logbuffer"
)

// gateLimit bounds every wait these tests make on the handler, so that a
// handler that never gets somewhere fails the test and does not hang it.
const gateLimit = 10 * time.Second

// gatedWriter is a ResponseWriter that stands for a client that is not
// reading: every write waits while the gate is shut. It starts shut.
type gatedWriter struct {
	header  http.Header
	waiting chan struct{} // closed when the first write waits at the gate
	first   sync.Once

	mu     sync.Mutex
	gate   chan struct{} // closed while writes may pass
	isOpen bool
	held   int   // writes waiting at the gate now
	frames int   // whole frames written, heartbeats not counted
	shutAt []int // frame counts after which the gate shuts again
	body   bytes.Buffer
}

func newGatedWriter() *gatedWriter {
	return &gatedWriter{header: http.Header{}, gate: make(chan struct{}), waiting: make(chan struct{})}
}

func (g *gatedWriter) Header() http.Header { return g.header }
func (g *gatedWriter) WriteHeader(int)     {}
func (g *gatedWriter) Flush()              {}

// open lets the client read.
func (g *gatedWriter) open() {
	g.mu.Lock()
	defer g.mu.Unlock()
	if !g.isOpen {
		g.isOpen = true
		close(g.gate)
	}
}

// shutAfter makes the client stop reading again once it has taken that
// many data frames in all.
func (g *gatedWriter) shutAfter(frames int) {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.shutAt = append(g.shutAt, frames)
}

// stoppedAt reports whether the client has taken exactly that many data
// frames and is not reading.
func (g *gatedWriter) stoppedAt(frames int) bool {
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.frames == frames && !g.isOpen
}

func (g *gatedWriter) Write(p []byte) (int, error) {
	g.first.Do(func() { close(g.waiting) })
	for {
		g.mu.Lock()
		gate := g.gate
		g.held++
		g.mu.Unlock()
		var late bool
		select {
		case <-gate:
		case <-time.After(gateLimit):
			late = true
		}
		g.mu.Lock()
		g.held--
		if late {
			g.mu.Unlock()
			return 0, errors.New("the test never opened the gate")
		}
		if !g.isOpen {
			// Shut again between the wait and the write.
			g.mu.Unlock()
			continue
		}
		n, err := g.body.Write(p)
		if string(p) == "\n\n" {
			g.frames++
			for _, at := range g.shutAt {
				if g.frames == at {
					g.isOpen = false
					g.gate = make(chan struct{})
				}
			}
		}
		g.mu.Unlock()
		return n, err
	}
}

// messages returns the message of every whole data frame written so far.
func (g *gatedWriter) messages(t *testing.T) []string {
	t.Helper()
	g.mu.Lock()
	written := g.body.String()
	g.mu.Unlock()
	whole, _, _ := cutLast(written, "\n\n")
	var out []string
	for _, frame := range strings.Split(whole, "\n\n") {
		payload, isData := strings.CutPrefix(frame, "data: ")
		if !isData {
			continue
		}
		var e logbuffer.Entry
		require.NoError(t, json.Unmarshal([]byte(payload), &e))
		out = append(out, e.Message)
	}
	return out
}

// cutLast is strings.Cut at the last sep.
func cutLast(s, sep string) (before, after string, found bool) {
	i := strings.LastIndex(s, sep)
	if i < 0 {
		return "", s, false
	}
	return s[:i], s[i+len(sep):], true
}

// stalledStream is a stream handler running against a gatedWriter.
type stalledStream struct {
	t    *testing.T
	w    *gatedWriter
	stop context.CancelFunc
	done chan struct{}
}

// openStalledStream starts the stream handler for the execution id with
// the given query and a client that does not read.
func openStalledStream(t *testing.T, srv *Server, id, query string) *stalledStream {
	t.Helper()
	ctx, stop := context.WithCancel(context.Background())
	req := httptest.NewRequest(http.MethodGet, "/api/v1/executions/"+id+"/logs/stream"+query, nil).WithContext(ctx)
	req.SetPathValue("id", id)
	st := &stalledStream{t: t, w: newGatedWriter(), stop: stop, done: make(chan struct{})}
	go func() {
		defer close(st.done)
		srv.handleStreamLogs(st.w, req)
	}()
	t.Cleanup(st.end)
	return st
}

// waitForAWrite returns once the handler is waiting to write a frame.
func (st *stalledStream) waitForAWrite() {
	st.t.Helper()
	select {
	case <-st.w.waiting:
	case <-time.After(gateLimit):
		require.FailNow(st.t, "the handler never tried to write a frame")
	}
}

// waitForAHeldWrite returns once the handler is waiting, now, to write
// to a client that is not reading.
func (st *stalledStream) waitForAHeldWrite() {
	st.t.Helper()
	held := func() bool {
		st.w.mu.Lock()
		defer st.w.mu.Unlock()
		return st.w.held > 0 && !st.w.isOpen
	}
	require.Eventually(st.t, held, gateLimit, time.Millisecond, "the handler is not waiting to write")
}

// waitStoppedAt returns once the client has taken that many data frames
// and stopped reading again (see shutAfter).
func (st *stalledStream) waitStoppedAt(frames int) {
	st.t.Helper()
	require.Eventually(st.t, func() bool { return st.w.stoppedAt(frames) }, gateLimit, time.Millisecond,
		"the client never got to %d frames", frames)
}

// end lets the client read again, hangs up and waits for the handler.
func (st *stalledStream) end() {
	st.t.Helper()
	st.w.open()
	st.stop()
	select {
	case <-st.done:
	case <-time.After(gateLimit):
		st.t.Error("the stream handler did not return after the client left")
	}
}

// messagesUntil waits for the frame that carries the message last and
// returns every message written up to it.
func (st *stalledStream) messagesUntil(last string) []string {
	st.t.Helper()
	var got []string
	arrived := func() bool {
		got = st.w.messages(st.t)
		return len(got) > 0 && got[len(got)-1] == last
	}
	if !assert.Eventually(st.t, arrived, gateLimit, 5*time.Millisecond) {
		require.FailNow(st.t, "the last entry never arrived",
			"waiting for %q, got %d messages ending with %v", last, len(got), tail(got, 3))
	}
	return got
}

func tail(s []string, n int) []string {
	if len(s) > n {
		return s[len(s)-n:]
	}
	return s
}

// observed gives srv a logger the test can read and returns its record.
func observed(srv *Server) *observer.ObservedLogs {
	core, logs := observer.New(zap.WarnLevel)
	srv.logger = zap.New(core)
	return logs
}

// droppedWarnings returns, for each WARN line about entries a stream
// lost, the stream it names with the two counts: "id dropped/total".
func droppedWarnings(logs *observer.ObservedLogs) []string {
	var out []string
	for _, line := range logs.FilterMessageSnippet("too slow").All() {
		fields := line.ContextMap()
		if _, filed := fields["execution_id"]; filed || line.Level != zap.WarnLevel {
			// A line with an execution id is filed in that run's log,
			// and so sent to the very reader that is behind.
			out = append(out, fmt.Sprintf("unexpected line: %v %v", line.Level, fields))
			continue
		}
		out = append(out, fmt.Sprintf("%v %v/%v", fields["stream"], fields["dropped"], fields["dropped_total"]))
	}
	return out
}

func appendWarn(t *testing.T, buf *logbuffer.Buffer, id, level, msg string) {
	t.Helper()
	// WARN and ERROR are never rate-limited, so every entry is admitted.
	require.Equal(t, logbuffer.DropNone, buf.Append(mkEntry(id, level, msg, time.Now().UTC(), nil)))
}
