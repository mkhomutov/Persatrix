// Tests for the SSE log streaming endpoint (RFC 0018 PR 5).
package server

import (
	"bufio"
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// startTestSSEServer wraps the Server's handler in a real httptest
// server so we can use a streaming http.Client (the in-process
// httptest.ResponseRecorder buffers writes and never delivers SSE
// frames mid-flight).
func startTestSSEServer(t *testing.T) (string, func()) {
	t.Helper()
	srv, _ := testServerWithBuffer(t)
	hs := httptest.NewServer(srv.Handler())
	return hs.URL, hs.Close
}

func TestSSE_NoBuffer_Returns501(t *testing.T) {
	srv, _ := testServer(t)
	hs := httptest.NewServer(srv.Handler())
	defer hs.Close()

	resp, err := http.Get(hs.URL + "/api/v1/executions/exec-1/logs/stream")
	require.NoError(t, err)
	defer resp.Body.Close()
	assert.Equal(t, http.StatusNotImplemented, resp.StatusCode)
}

func TestSSE_InvalidExecutionID_Returns400(t *testing.T) {
	url, stop := startTestSSEServer(t)
	defer stop()

	resp, err := http.Get(url + "/api/v1/executions/bad..id/logs/stream")
	require.NoError(t, err)
	defer resp.Body.Close()
	assert.Equal(t, http.StatusBadRequest, resp.StatusCode)
}

func TestSSE_StreamsAppendedEntries(t *testing.T) {
	srv, buf := testServerWithBuffer(t)
	hs := httptest.NewServer(srv.Handler())
	defer hs.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet,
		hs.URL+"/api/v1/executions/exec-1/logs/stream", nil)
	require.NoError(t, err)
	resp, err := http.DefaultClient.Do(req)
	require.NoError(t, err)
	defer resp.Body.Close()
	require.Equal(t, http.StatusOK, resp.StatusCode)
	assert.Equal(t, "text/event-stream", resp.Header.Get("Content-Type"))
	assert.Equal(t, "no", resp.Header.Get("X-Accel-Buffering"))

	// Producer goroutine — append after the subscriber is attached.
	// A small sleep gives the handler's Subscribe call time to land
	// before the broadcast; without it the entry can be appended
	// before the subscriber is registered and the test races.
	go func() {
		time.Sleep(50 * time.Millisecond)
		buf.Append(mkEntry("exec-1", "INFO", "hello-sse", time.Now().UTC(), nil))
	}()

	reader := bufio.NewReader(resp.Body)
	var dataLine string
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		line, err := reader.ReadString('\n')
		if err != nil {
			break
		}
		if strings.HasPrefix(line, "data: ") {
			dataLine = strings.TrimSpace(strings.TrimPrefix(line, "data: "))
			break
		}
	}
	require.NotEmpty(t, dataLine, "expected at least one data: frame")
	assert.Contains(t, dataLine, `"hello-sse"`)
}

// Compound case exercised by the README Quick Start
// (`persatrix logs _ --follow`): subscribing to the stream endpoint with
// the `_` wildcard must fan-out entries from *every* execution, not just
// one. Mirrors TestLogs_CrossExecutionMerge in logs_handler_test.go but
// exercises the SSE path that the handler's `if id == crossExecutionToken`
// branch (logs_stream_handler.go) translates to Subscribe("").
func TestSSE_CrossExecutionMerge_StreamsAllExecutions(t *testing.T) {
	srv, buf := testServerWithBuffer(t)
	hs := httptest.NewServer(srv.Handler())
	defer hs.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet,
		hs.URL+"/api/v1/executions/_/logs/stream", nil)
	require.NoError(t, err)
	resp, err := http.DefaultClient.Do(req)
	require.NoError(t, err)
	defer resp.Body.Close()
	require.Equal(t, http.StatusOK, resp.StatusCode)

	// Append entries to two *different* executions after the subscriber
	// is attached. Both must reach the wildcard subscriber.
	go func() {
		time.Sleep(50 * time.Millisecond)
		now := time.Now().UTC()
		buf.Append(mkEntry("exec-A", "INFO", "from-a", now, nil))
		buf.Append(mkEntry("exec-B", "INFO", "from-b", now.Add(time.Millisecond), nil))
	}()

	reader := bufio.NewReader(resp.Body)
	var sawA, sawB bool
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) && !(sawA && sawB) {
		line, err := reader.ReadString('\n')
		if err != nil {
			break
		}
		if !strings.HasPrefix(line, "data: ") {
			continue
		}
		payload := strings.TrimSpace(strings.TrimPrefix(line, "data: "))
		if strings.Contains(payload, `"from-a"`) {
			sawA = true
		}
		if strings.Contains(payload, `"from-b"`) {
			sawB = true
		}
	}
	assert.True(t, sawA, "expected entry from exec-A on the wildcard stream")
	assert.True(t, sawB, "expected entry from exec-B on the wildcard stream")
}

// Issue #179 Should-Fix #3: sseWrite must gracefully degrade when the
// underlying ResponseWriter doesn't support SetWriteDeadline (test
// doubles, middleware without deadline support).  ErrNotSupported must
// not surface as a write failure — the write still proceeds.
func TestSSEWrite_DeadlineUnsupportedStillWrites(t *testing.T) {
	rec := httptest.NewRecorder()
	rc := http.NewResponseController(rec)
	require.NoError(t, sseWrite(rc, rec, []byte("data: hello\n\n")))
	assert.Equal(t, "data: hello\n\n", rec.Body.String())
}
