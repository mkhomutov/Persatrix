package server

import (
	"context"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/planner"
	"github.com/mkhomutov/persatrix/internal/registry"
	"github.com/mkhomutov/persatrix/internal/state"
)

// --- New / Constructor Tests ---

func TestNewValidatesWorkflowsDir(t *testing.T) {
	logger := zap.NewNop()
	store := state.NewInMemoryStore(logger)
	reg := registry.NewInMemoryRegistry(logger)
	pl := planner.NewYAMLPlanner(logger)

	_, err := New("127.0.0.1:0", "/nonexistent-path-xyz", store, reg, pl, logger)
	assert.Error(t, err)
	assert.Contains(t, err.Error(), "not accessible")
}

func TestNewRejectsFilePath(t *testing.T) {
	dir := t.TempDir()
	f := filepath.Join(dir, "file.txt")
	require.NoError(t, os.WriteFile(f, []byte("hi"), 0644))

	logger := zap.NewNop()
	_, err := New("127.0.0.1:0", f, state.NewInMemoryStore(logger), registry.NewInMemoryRegistry(logger), planner.NewYAMLPlanner(logger), logger)
	assert.Error(t, err)
	assert.Contains(t, err.Error(), "not a directory")
}

func TestNewNilLogger(t *testing.T) {
	dir := t.TempDir()
	srv, err := New("127.0.0.1:0", dir, state.NewInMemoryStore(nil), registry.NewInMemoryRegistry(nil), planner.NewYAMLPlanner(nil), nil)
	require.NoError(t, err)
	assert.NotNil(t, srv)
}

// --- Healthz ---

func TestHealthz(t *testing.T) {
	srv, _ := testServer(t)
	rec := doRequest(srv.Handler(), http.MethodGet, "/healthz", nil)
	assert.Equal(t, http.StatusOK, rec.Code)
	assert.Contains(t, rec.Body.String(), `"status":"ok"`)
}

// --- Request ID Header ---

func TestRequestIDHeader(t *testing.T) {
	srv, _ := testServer(t)
	rec := doRequest(srv.Handler(), http.MethodGet, "/healthz", nil)
	assert.NotEmpty(t, rec.Header().Get("X-Request-ID"))
}

func TestRequestIDNotEchoed(t *testing.T) {
	srv, _ := testServer(t)
	req := httptest.NewRequest(http.MethodGet, "/healthz", nil)
	req.Header.Set("X-Request-ID", "client-injected-id")
	rec := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, req)
	// Server must generate its own, not echo client's
	assert.NotEqual(t, "client-injected-id", rec.Header().Get("X-Request-ID"))
	assert.NotEmpty(t, rec.Header().Get("X-Request-ID"))
}

// --- Method Not Allowed (T-06) ---

func TestMethodNotAllowed(t *testing.T) {
	srv, _ := testServer(t)
	rec := doRequest(srv.Handler(), http.MethodPut, "/api/v1/workflows/run", nil)
	assert.Equal(t, http.StatusMethodNotAllowed, rec.Code)
}

// --- Panic Recovery ---

func TestPanicRecovery(t *testing.T) {
	logger := zap.NewNop()
	mux := http.NewServeMux()
	mux.HandleFunc("GET /panic", func(w http.ResponseWriter, r *http.Request) {
		panic("test panic")
	})
	handler := recoveryMiddleware(logger, mux)

	req := httptest.NewRequest(http.MethodGet, "/panic", nil)
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	assert.Equal(t, http.StatusInternalServerError, rec.Code)
	assert.Contains(t, rec.Body.String(), "internal server error")
}

// TestPanicRecoveryFullChain validates that panic recovery works through the
// complete middleware chain (recovery → requestID → logging → handler),
// ensuring the X-Request-ID header is present on panic-recovered responses
// and the JSON error envelope is correctly formed.
// (Review finding F-04: the isolated test above does not prove the full chain.)
func TestPanicRecoveryFullChain(t *testing.T) {
	srv, _ := testServer(t)
	srv.mux.HandleFunc("GET /test-panic", func(w http.ResponseWriter, r *http.Request) {
		panic("test panic")
	})
	rec := doRequest(srv.Handler(), http.MethodGet, "/test-panic", nil)
	assert.Equal(t, http.StatusInternalServerError, rec.Code)
	assert.Contains(t, rec.Body.String(), "internal server error")
	assert.NotEmpty(t, rec.Header().Get("X-Request-ID"),
		"panic-recovered responses must include X-Request-ID from the full middleware chain")
}

// --- Graceful Shutdown ---

func TestStartAndGracefulShutdown(t *testing.T) {
	srv, _ := testServer(t)
	// Use a random available port
	srv.addr = "127.0.0.1:0"

	ctx, cancel := context.WithCancel(context.Background())
	errCh := make(chan error, 1)
	go func() {
		errCh <- srv.Start(ctx)
	}()

	// NOTE (Review finding F-05): Sleep-based synchronization is a known flake
	// source. A retry-dial loop would be more robust but requires Start() to expose
	// the actual bound address, which it doesn't in v0.1. The 100ms budget is generous
	// for a loopback TCP bind. If this becomes flaky in CI, increase the sleep or
	// refactor Start() to accept a net.Listener.
	time.Sleep(100 * time.Millisecond)
	cancel()

	err := <-errCh
	assert.NoError(t, err)
}

// --- Start error propagation (T-02) ---

func TestStartErrorOnPortInUse(t *testing.T) {
	// (Review finding T-02): Validates the errCh-based pattern in Start() handles
	// the bind failure path (ListenAndServe returns immediately with an error).
	srv, _ := testServer(t)

	// Bind a listener to grab a port.
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	require.NoError(t, err)
	defer ln.Close()

	// Point the server at the already-bound port.
	srv.addr = ln.Addr().String()

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()

	err = srv.Start(ctx)
	assert.Error(t, err, "Start should return an error when the port is already in use")
}

// --- statusCapture.Flush() (F-06) ---

// TestStatusCaptureFlush validates that the Flush() method on statusCapture
// correctly delegates to the underlying ResponseWriter when it implements
// http.Flusher. This was added for v0.2 SSE forward-compatibility.
// (Review finding F-06: 0% coverage on Flush() method.)
func TestStatusCaptureFlush(t *testing.T) {
	rec := httptest.NewRecorder() // implements http.Flusher
	sc := &statusCapture{ResponseWriter: rec, status: http.StatusOK}
	sc.Flush() // should not panic
	assert.True(t, rec.Flushed)
}

// --- statusCapture.Unwrap() (F-04) ---

// TestStatusCaptureUnwrap validates that Unwrap() returns the underlying
// ResponseWriter so Go 1.20+ http.ResponseController can discover optional
// interfaces (http.Flusher, http.Hijacker) via the standard unwrapping protocol.
// (Deep review finding F-04: 0% coverage on Unwrap() method.)
func TestStatusCaptureUnwrap(t *testing.T) {
	rec := httptest.NewRecorder()
	sc := &statusCapture{ResponseWriter: rec}
	assert.Equal(t, rec, sc.Unwrap())
}
