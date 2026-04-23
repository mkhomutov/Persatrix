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
