package server

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/planner"
	"github.com/mkhomutov/persatrix/internal/registry"
	"github.com/mkhomutov/persatrix/internal/state"
)

// testServer creates a Server backed by in-memory store/registry and a real
// YAMLPlanner pointing at a temp workflows directory. Returns the server and
// the temp dir path for placing workflow fixtures.
func testServer(t *testing.T) (*Server, string) {
	t.Helper()
	dir := t.TempDir()
	logger := zap.NewNop()
	store := state.NewInMemoryStore(logger)
	reg := registry.NewInMemoryRegistry(logger)
	pl := planner.NewYAMLPlanner(logger)
	srv, err := New("127.0.0.1:0", dir, store, reg, pl, logger)
	require.NoError(t, err)
	return srv, dir
}

// writeWorkflowFixture writes a valid workflow YAML file into the temp dir.
func writeWorkflowFixture(t *testing.T, dir, id string) {
	t.Helper()
	content := `schema_version: "0.1"
workflow:
  id: "` + id + `"
  name: "Test Workflow"
  trigger: "manual"
  steps:
    - id: "step-one"
      agent: "test-agent"
      input: "hello"
      output_key: "result"
`
	err := os.WriteFile(filepath.Join(dir, id+".yaml"), []byte(content), 0644)
	require.NoError(t, err)
}

func doRequest(handler http.Handler, method, path string, body []byte) *httptest.ResponseRecorder {
	var req *http.Request
	if body != nil {
		req = httptest.NewRequest(method, path, bytes.NewReader(body))
		req.Header.Set("Content-Type", "application/json")
	} else {
		req = httptest.NewRequest(method, path, nil)
	}
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	return rec
}

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

// --- Submit Workflow Run ---

func TestSubmitWorkflowRun(t *testing.T) {
	srv, dir := testServer(t)
	writeWorkflowFixture(t, dir, "test-workflow")

	body, _ := json.Marshal(submitWorkflowRunRequest{
		WorkflowID: "test-workflow",
		Inputs:     map[string]string{"key": "val"},
	})
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/workflows/run", body)
	assert.Equal(t, http.StatusCreated, rec.Code)

	var resp submitWorkflowRunResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	assert.NotEmpty(t, resp.RunID)
	assert.Equal(t, "test-workflow", resp.WorkflowID)
	assert.Equal(t, "pending", resp.Status)
}

func TestSubmitWorkflowRunMissingWorkflowID(t *testing.T) {
	srv, _ := testServer(t)
	body := []byte(`{"inputs": {"k": "v"}}`)
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/workflows/run", body)
	assert.Equal(t, http.StatusBadRequest, rec.Code)
	assert.Contains(t, rec.Body.String(), "workflow_id is required")
}

func TestSubmitWorkflowRunEmptyWorkflowID(t *testing.T) {
	srv, _ := testServer(t)
	body := []byte(`{"workflow_id": ""}`)
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/workflows/run", body)
	assert.Equal(t, http.StatusBadRequest, rec.Code)
	assert.Contains(t, rec.Body.String(), "workflow_id is required")
}

func TestSubmitWorkflowRunInvalidWorkflowID(t *testing.T) {
	srv, _ := testServer(t)
	tests := []struct {
		name string
		id   string
	}{
		{"uppercase", "Test-Workflow"},
		{"underscore", "test_workflow"},
		{"single char", "a"},
		{"starts with dash", "-test"},
		{"ends with dash", "test-"},
		{"with dots", "test.workflow"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			body, _ := json.Marshal(submitWorkflowRunRequest{WorkflowID: tc.id})
			rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/workflows/run", body)
			assert.Equal(t, http.StatusBadRequest, rec.Code)
		})
	}
}

func TestSubmitWorkflowRunNotFound(t *testing.T) {
	srv, _ := testServer(t)
	body, _ := json.Marshal(submitWorkflowRunRequest{WorkflowID: "no-such-workflow"})
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/workflows/run", body)
	assert.Equal(t, http.StatusNotFound, rec.Code)
}

func TestSubmitWorkflowRunWrongContentType(t *testing.T) {
	srv, _ := testServer(t)
	req := httptest.NewRequest(http.MethodPost, "/api/v1/workflows/run", strings.NewReader(`{}`))
	req.Header.Set("Content-Type", "text/plain")
	rec := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, req)
	assert.Equal(t, http.StatusBadRequest, rec.Code)
	assert.Contains(t, rec.Body.String(), "Content-Type must be application/json")
}

func TestSubmitWorkflowRunMalformedJSON(t *testing.T) {
	srv, _ := testServer(t)
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/workflows/run", []byte(`{invalid}`))
	assert.Equal(t, http.StatusBadRequest, rec.Code)
	assert.Contains(t, rec.Body.String(), "invalid or malformed JSON body")
}

func TestSubmitWorkflowRunEmptyBody(t *testing.T) {
	srv, _ := testServer(t)
	req := httptest.NewRequest(http.MethodPost, "/api/v1/workflows/run", strings.NewReader(""))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, req)
	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

func TestSubmitWorkflowRunUnknownField(t *testing.T) {
	srv, _ := testServer(t)
	body := []byte(`{"workflow_id": "test-wf", "unknown_field": "bad"}`)
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/workflows/run", body)
	assert.Equal(t, http.StatusBadRequest, rec.Code)
	assert.Contains(t, rec.Body.String(), "invalid or malformed JSON body")
}

func TestSubmitWorkflowRunBodyTooLarge(t *testing.T) {
	srv, _ := testServer(t)
	// Build a valid-ish JSON object that exceeds 1 MiB.
	// A long string value forces the JSON decoder to read past the limit.
	bigValue := strings.Repeat("x", (1<<20)+100)
	body := []byte(`{"workflow_id":"` + bigValue + `"}`)
	req := httptest.NewRequest(http.MethodPost, "/api/v1/workflows/run", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, req)
	assert.Equal(t, http.StatusBadRequest, rec.Code)
	assert.Contains(t, rec.Body.String(), "too large")
}

// --- Path Traversal ---

func TestPathTraversalDotDot(t *testing.T) {
	srv, _ := testServer(t)
	body, _ := json.Marshal(submitWorkflowRunRequest{WorkflowID: "../etc/passwd"})
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/workflows/run", body)
	// Invalid ID format (contains / and .)
	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

// --- Get Workflow Status ---

func TestGetWorkflowStatus(t *testing.T) {
	srv, dir := testServer(t)
	writeWorkflowFixture(t, dir, "test-wf")

	// Submit a run first
	body, _ := json.Marshal(submitWorkflowRunRequest{WorkflowID: "test-wf"})
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/workflows/run", body)
	require.Equal(t, http.StatusCreated, rec.Code)
	var createResp submitWorkflowRunResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &createResp))

	// Get status
	rec = doRequest(srv.Handler(), http.MethodGet, "/api/v1/workflows/"+createResp.RunID+"/status", nil)
	assert.Equal(t, http.StatusOK, rec.Code)

	var statusResp workflowRunResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &statusResp))
	assert.Equal(t, createResp.RunID, statusResp.RunID)
	assert.Equal(t, "test-wf", statusResp.WorkflowID)
	assert.Equal(t, "pending", statusResp.Status)
	assert.NotNil(t, statusResp.StartedAt)
	assert.Nil(t, statusResp.FinishedAt)
	assert.NotNil(t, statusResp.Steps)
}

func TestGetWorkflowStatusNotFound(t *testing.T) {
	srv, _ := testServer(t)
	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/workflows/nonexistent-id/status", nil)
	// "nonexistent-id" is not a valid UUID, so it should be rejected at the
	// format validation layer before reaching the store.
	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

// --- List Workflows ---

func TestListWorkflowsEmpty(t *testing.T) {
	srv, _ := testServer(t)
	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/workflows", nil)
	assert.Equal(t, http.StatusOK, rec.Code)
	assert.Equal(t, "[]\n", rec.Body.String())
}

func TestListWorkflowsWithRuns(t *testing.T) {
	srv, dir := testServer(t)
	writeWorkflowFixture(t, dir, "test-wf")

	body, _ := json.Marshal(submitWorkflowRunRequest{WorkflowID: "test-wf"})
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/workflows/run", body)
	require.Equal(t, http.StatusCreated, rec.Code)

	rec = doRequest(srv.Handler(), http.MethodGet, "/api/v1/workflows", nil)
	assert.Equal(t, http.StatusOK, rec.Code)

	var list []workflowRunResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &list))
	assert.Len(t, list, 1)
	assert.Equal(t, "test-wf", list[0].WorkflowID)
}

// --- Delete Workflow ---

func TestDeleteWorkflow(t *testing.T) {
	srv, dir := testServer(t)
	writeWorkflowFixture(t, dir, "test-wf")

	// Submit
	body, _ := json.Marshal(submitWorkflowRunRequest{WorkflowID: "test-wf"})
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/workflows/run", body)
	require.Equal(t, http.StatusCreated, rec.Code)
	var createResp submitWorkflowRunResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &createResp))

	// Delete
	rec = doRequest(srv.Handler(), http.MethodDelete, "/api/v1/workflows/"+createResp.RunID, nil)
	assert.Equal(t, http.StatusNoContent, rec.Code)

	// Verify gone
	rec = doRequest(srv.Handler(), http.MethodGet, "/api/v1/workflows/"+createResp.RunID+"/status", nil)
	assert.Equal(t, http.StatusNotFound, rec.Code)
}

func TestDeleteWorkflowNotFound(t *testing.T) {
	srv, _ := testServer(t)
	// "nonexistent-id" is not a valid UUID — rejected at format validation.
	rec := doRequest(srv.Handler(), http.MethodDelete, "/api/v1/workflows/nonexistent-id", nil)
	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

func TestDeleteRunningWorkflowConflict(t *testing.T) {
	srv, dir := testServer(t)
	writeWorkflowFixture(t, dir, "test-wf")

	// Submit
	body, _ := json.Marshal(submitWorkflowRunRequest{WorkflowID: "test-wf"})
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/workflows/run", body)
	require.Equal(t, http.StatusCreated, rec.Code)
	var createResp submitWorkflowRunResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &createResp))

	// Advance to Running via store
	store := srv.store
	err := store.UpdateRunStatus(context.Background(), createResp.RunID, state.RunRunning)
	require.NoError(t, err)

	// Delete should fail with 409
	rec = doRequest(srv.Handler(), http.MethodDelete, "/api/v1/workflows/"+createResp.RunID, nil)
	assert.Equal(t, http.StatusConflict, rec.Code)
	assert.Contains(t, rec.Body.String(), "cannot delete a running workflow run")
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

// --- Full Lifecycle ---

func TestWorkflowRunLifecycle(t *testing.T) {
	srv, dir := testServer(t)
	writeWorkflowFixture(t, dir, "lifecycle-wf")
	h := srv.Handler()

	// 1. POST run → 201
	body, _ := json.Marshal(submitWorkflowRunRequest{WorkflowID: "lifecycle-wf", Inputs: map[string]string{"user_request": "test"}})
	rec := doRequest(h, http.MethodPost, "/api/v1/workflows/run", body)
	require.Equal(t, http.StatusCreated, rec.Code)
	var createResp submitWorkflowRunResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &createResp))
	runID := createResp.RunID

	// 2. GET status → 200, pending
	rec = doRequest(h, http.MethodGet, "/api/v1/workflows/"+runID+"/status", nil)
	assert.Equal(t, http.StatusOK, rec.Code)
	var statusResp workflowRunResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &statusResp))
	assert.Equal(t, "pending", statusResp.Status)

	// 3. GET list → contains the run
	rec = doRequest(h, http.MethodGet, "/api/v1/workflows", nil)
	assert.Equal(t, http.StatusOK, rec.Code)
	var list []workflowRunResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &list))
	assert.Len(t, list, 1)

	// 4. DELETE → 204
	rec = doRequest(h, http.MethodDelete, "/api/v1/workflows/"+runID, nil)
	assert.Equal(t, http.StatusNoContent, rec.Code)

	// 5. GET again → 404
	rec = doRequest(h, http.MethodGet, "/api/v1/workflows/"+runID+"/status", nil)
	assert.Equal(t, http.StatusNotFound, rec.Code)
}

// --- Concurrent Access ---

func TestConcurrentAccess(t *testing.T) {
	srv, dir := testServer(t)
	writeWorkflowFixture(t, dir, "concurrent-wf")
	h := srv.Handler()

	done := make(chan struct{})
	for i := 0; i < 20; i++ {
		go func() {
			defer func() { done <- struct{}{} }()
			body, _ := json.Marshal(submitWorkflowRunRequest{WorkflowID: "concurrent-wf"})
			rec := doRequest(h, http.MethodPost, "/api/v1/workflows/run", body)
			assert.Equal(t, http.StatusCreated, rec.Code)
		}()
	}
	for i := 0; i < 20; i++ {
		<-done
	}

	// Verify all 20 runs exist
	rec := doRequest(h, http.MethodGet, "/api/v1/workflows", nil)
	var list []workflowRunResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &list))
	assert.Len(t, list, 20)
}

// --- runStatusString ---

func TestRunStatusString(t *testing.T) {
	tests := []struct {
		status state.RunStatus
		want   string
	}{
		{state.RunPending, "pending"},
		{state.RunRunning, "running"},
		{state.RunCompleted, "completed"},
		{state.RunFailed, "failed"},
		{state.RunCancelled, "cancelled"},
		{state.RunRetrying, "retrying"},
		{state.RunStatus(99), "unknown"},
	}
	for _, tc := range tests {
		assert.Equal(t, tc.want, runStatusString(tc.status))
	}
}

// --- runToResponse with FinishedAt ---

func TestRunToResponseWithFinishedAt(t *testing.T) {
	now := time.Now()
	run := &state.WorkflowRun{
		ID:         "test-id",
		WorkflowID: "test-wf",
		Status:     state.RunCompleted,
		StartedAt:  now,
		FinishedAt: now.Add(5 * time.Second),
		Steps:      map[string]state.StepState{},
	}
	resp := runToResponse(run)
	assert.Equal(t, "completed", resp.Status)
	assert.NotNil(t, resp.StartedAt)
	assert.NotNil(t, resp.FinishedAt)
}

// --- Content-Type Handling (T-02) ---

func TestSubmitWorkflowRunContentTypeWithCharset(t *testing.T) {
	srv, dir := testServer(t)
	writeWorkflowFixture(t, dir, "test-wf")
	body, _ := json.Marshal(submitWorkflowRunRequest{WorkflowID: "test-wf"})
	req := httptest.NewRequest(http.MethodPost, "/api/v1/workflows/run", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json; charset=utf-8")
	rec := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, req)
	assert.Equal(t, http.StatusCreated, rec.Code)
}

// --- Non-String Input Values (T-01) ---

func TestSubmitWorkflowRunNonStringInputs(t *testing.T) {
	srv, dir := testServer(t)
	writeWorkflowFixture(t, dir, "test-wf")
	// inputs.key is a number, but map[string]string requires all values to be strings.
	body := []byte(`{"workflow_id": "test-wf", "inputs": {"key": 42}}`)
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/workflows/run", body)
	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

// --- Method Not Allowed (T-06) ---

func TestMethodNotAllowed(t *testing.T) {
	srv, _ := testServer(t)
	rec := doRequest(srv.Handler(), http.MethodPut, "/api/v1/workflows/run", nil)
	assert.Equal(t, http.StatusMethodNotAllowed, rec.Code)
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

// --- DeleteWorkflow: delete completed/failed/cancelled statuses ---

func TestDeleteNonRunningStatuses(t *testing.T) {
	// RunRetrying included for parity with state-level TestDeleteRunAnyStatus (N-19).
	statuses := []state.RunStatus{state.RunCompleted, state.RunFailed, state.RunCancelled, state.RunRetrying}
	for _, status := range statuses {
		t.Run(runStatusString(status), func(t *testing.T) {
			srv, dir := testServer(t)
			writeWorkflowFixture(t, dir, "test-wf")
			h := srv.Handler()

			body, _ := json.Marshal(submitWorkflowRunRequest{WorkflowID: "test-wf"})
			rec := doRequest(h, http.MethodPost, "/api/v1/workflows/run", body)
			require.Equal(t, http.StatusCreated, rec.Code)
			var cr submitWorkflowRunResponse
			require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &cr))

			require.NoError(t, srv.store.UpdateRunStatus(context.Background(), cr.RunID, status))

			rec = doRequest(h, http.MethodDelete, "/api/v1/workflows/"+cr.RunID, nil)
			assert.Equal(t, http.StatusNoContent, rec.Code)
		})
	}
}

// --- Workflow parse error (invalid YAML) ---

func TestSubmitWorkflowRunParseError(t *testing.T) {
	srv, dir := testServer(t)
	// Write an invalid workflow YAML
	err := os.WriteFile(filepath.Join(dir, "bad-wf.yaml"), []byte("not: valid: workflow"), 0644)
	require.NoError(t, err)

	body, _ := json.Marshal(submitWorkflowRunRequest{WorkflowID: "bad-wf"})
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/workflows/run", body)
	assert.Equal(t, http.StatusUnprocessableEntity, rec.Code)
	assert.Contains(t, rec.Body.String(), "UNPROCESSABLE")
	// (Review finding F-10): error message must NOT leak filesystem paths or YAML internals.
	assert.Contains(t, rec.Body.String(), "workflow file could not be parsed")
	assert.NotContains(t, rec.Body.String(), dir, "response must not leak filesystem paths")
}

// --- Workflow DAG validation error (cycle) ---

func TestSubmitWorkflowRunDAGError(t *testing.T) {
	srv, dir := testServer(t)
	cycle := `schema_version: "0.1"
workflow:
  id: "cycle-wf"
  name: "Cycle"
  trigger: "manual"
  steps:
    - id: "step-a"
      agent: "test-agent"
      input: "hello"
      output_key: "a"
      depends_on: ["step-b"]
    - id: "step-b"
      agent: "test-agent"
      input: "hello"
      output_key: "b"
      depends_on: ["step-a"]
`
	require.NoError(t, os.WriteFile(filepath.Join(dir, "cycle-wf.yaml"), []byte(cycle), 0644))

	body, _ := json.Marshal(submitWorkflowRunRequest{WorkflowID: "cycle-wf"})
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/workflows/run", body)
	assert.Equal(t, http.StatusUnprocessableEntity, rec.Code)
	assert.Contains(t, rec.Body.String(), "UNPROCESSABLE")
	// (Review finding F-11): error message must NOT leak internal step IDs or dependency structure.
	assert.Contains(t, rec.Body.String(), "workflow contains invalid dependencies")
	assert.NotContains(t, rec.Body.String(), "step-a", "response must not leak step IDs")
	assert.NotContains(t, rec.Body.String(), "step-b", "response must not leak step IDs")
}

// --- resolveWorkflowPath: file outside directory (symlink) ---

func TestResolveWorkflowPathPrefixCheck(t *testing.T) {
	srv, _ := testServer(t)
	// Direct call to resolveWorkflowPath with a valid-format but nonexistent ID
	_, err := srv.resolveWorkflowPath("nonexistent-wf")
	assert.ErrorIs(t, err, ErrWorkflowNotFound)
}

// --- Inputs with nil ---

func TestSubmitWorkflowRunNoInputs(t *testing.T) {
	srv, dir := testServer(t)
	writeWorkflowFixture(t, dir, "test-wf")

	body := []byte(`{"workflow_id": "test-wf"}`)
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/workflows/run", body)
	assert.Equal(t, http.StatusCreated, rec.Code)
}

// --- resolveWorkflowPath: symlink escape (T-01) ---

func TestResolveWorkflowPathSymlinkEscape(t *testing.T) {
	// (Review finding T-01): The RFC explicitly requires testing a symlink pointing
	// outside the workflows directory. This validates the most critical security
	// property of the 3-layer path traversal defense (regex + EvalSymlinks + HasPrefix).
	if os.Getenv("CI") != "" && os.Getenv("RUNNER_OS") == "Windows" {
		t.Skip("symlink creation may require elevated privileges on Windows CI")
	}

	dir := t.TempDir()
	outsideDir := t.TempDir()

	// Create a file outside the workflows directory.
	outsideFile := filepath.Join(outsideDir, "secret-wf.yaml")
	require.NoError(t, os.WriteFile(outsideFile, []byte("schema_version: \"0.1\"\n"), 0644))

	// Create a symlink inside the workflows directory pointing to the outside file.
	symlinkPath := filepath.Join(dir, "escape-wf.yaml")
	err := os.Symlink(outsideFile, symlinkPath)
	if err != nil {
		t.Skipf("cannot create symlink (likely unprivileged on Windows): %v", err)
	}

	logger := zap.NewNop()
	srv, err := New("127.0.0.1:0", dir,
		state.NewInMemoryStore(logger), registry.NewInMemoryRegistry(logger),
		planner.NewYAMLPlanner(logger), logger)
	require.NoError(t, err)

	// resolveWorkflowPath must reject the symlink-escaped path.
	_, err = srv.resolveWorkflowPath("escape-wf")
	assert.ErrorIs(t, err, ErrWorkflowNotFound)
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

// --- Missing Content-Type header (F-07) ---

// TestSubmitWorkflowRunNoContentType validates that requests with no Content-Type
// header are rejected. mime.ParseMediaType("") returns an error, so requireJSON
// correctly rejects it, but this edge case deserves explicit coverage.
// (Review finding F-07.)
func TestSubmitWorkflowRunNoContentType(t *testing.T) {
	srv, _ := testServer(t)
	req := httptest.NewRequest(http.MethodPost, "/api/v1/workflows/run", strings.NewReader(`{}`))
	// No Content-Type header set
	rec := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, req)
	assert.Equal(t, http.StatusBadRequest, rec.Code)
	assert.Contains(t, rec.Body.String(), "Content-Type must be application/json")
}

// --- Run ID format validation (F-02) ---

// TestGetWorkflowStatusInvalidRunID validates that non-UUID run IDs are rejected
// at the handler boundary before reaching the store layer.
// (Review finding F-02: validate at system boundaries.)
func TestGetWorkflowStatusInvalidRunID(t *testing.T) {
	srv, _ := testServer(t)
	// Review finding F-07: assert the exact expected status (400) instead of
	// only checking "not 404". The path traversal case is omitted because
	// ServeMux cleans the double-dot segments before routing, so the request
	// never reaches handleGetWorkflowStatus — it tests mux normalization,
	// not validateRunID.
	tests := []struct {
		name string
		id   string
	}{
		{"too long", strings.Repeat("a", 100)},
		{"not a UUID", "not-a-uuid-at-all"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/workflows/"+tc.id+"/status", nil)
			assert.Equal(t, http.StatusBadRequest, rec.Code,
				"non-UUID IDs should be rejected with 400 before reaching the store")
		})
	}
}

func TestDeleteWorkflowInvalidRunID(t *testing.T) {
	srv, _ := testServer(t)
	// Review finding F-02: previous test used /api/v1/workflows/not-a-uuid/status
	// which hit the GET status route with wrong method (405), not the DELETE handler.
	rec := doRequest(srv.Handler(), http.MethodDelete, "/api/v1/workflows/not-a-uuid", nil)
	assert.Equal(t, http.StatusBadRequest, rec.Code,
		"non-UUID IDs should be rejected with 400 before reaching the store")
}

// TestGetWorkflowStatusValidUUIDNotFound validates that a valid UUID that doesn't
// exist in the store returns 404 (not 400).
func TestGetWorkflowStatusValidUUIDNotFound(t *testing.T) {
	srv, _ := testServer(t)
	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/workflows/00000000-0000-0000-0000-000000000000/status", nil)
	assert.Equal(t, http.StatusNotFound, rec.Code)
}

func TestDeleteWorkflowValidUUIDNotFound(t *testing.T) {
	srv, _ := testServer(t)
	rec := doRequest(srv.Handler(), http.MethodDelete, "/api/v1/workflows/00000000-0000-0000-0000-000000000000", nil)
	assert.Equal(t, http.StatusNotFound, rec.Code)
}

// =============================================================================
// Agent Handler Tests
// =============================================================================

// --- Register Agent ---

func TestRegisterAgent(t *testing.T) {
	srv, _ := testServer(t)
	body := []byte(`{"id": "code-writer", "address": "localhost:50051", "capabilities": ["code_generation", "code_review"]}`)
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/agents/register", body)
	assert.Equal(t, http.StatusCreated, rec.Code)

	var resp agentResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	assert.Equal(t, "code-writer", resp.ID)
	assert.Equal(t, "localhost:50051", resp.Address)
	assert.Equal(t, []string{"code_generation", "code_review"}, resp.Capabilities)
	assert.Equal(t, "healthy", resp.Status)
}

func TestRegisterAgentMissingID(t *testing.T) {
	srv, _ := testServer(t)
	body := []byte(`{"address": "localhost:50051"}`)
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/agents/register", body)
	assert.Equal(t, http.StatusBadRequest, rec.Code)
	assert.Contains(t, rec.Body.String(), "id is required")
}

func TestRegisterAgentInvalidID(t *testing.T) {
	srv, _ := testServer(t)
	tests := []struct {
		name string
		id   string
	}{
		{"uppercase", "Code-Writer"},
		{"underscore", "code_writer"},
		{"single char", "a"},
		{"starts with dash", "-writer"},
		{"ends with dash", "writer-"},
		{"with dots", "code.writer"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			body, _ := json.Marshal(registerAgentRequest{ID: tc.id, Address: "localhost:50051"})
			rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/agents/register", body)
			assert.Equal(t, http.StatusBadRequest, rec.Code)
		})
	}
}

func TestRegisterAgentEmptyAddress(t *testing.T) {
	srv, _ := testServer(t)
	body := []byte(`{"id": "test-agent", "address": ""}`)
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/agents/register", body)
	assert.Equal(t, http.StatusBadRequest, rec.Code)
	assert.Contains(t, rec.Body.String(), "address is required")
}

func TestRegisterAgentMissingAddress(t *testing.T) {
	srv, _ := testServer(t)
	body := []byte(`{"id": "test-agent"}`)
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/agents/register", body)
	assert.Equal(t, http.StatusBadRequest, rec.Code)
	assert.Contains(t, rec.Body.String(), "address is required")
}

func TestRegisterAgentDuplicate(t *testing.T) {
	srv, _ := testServer(t)
	body := []byte(`{"id": "code-writer", "address": "localhost:50051"}`)
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/agents/register", body)
	require.Equal(t, http.StatusCreated, rec.Code)

	// Second registration with same ID → 409
	rec = doRequest(srv.Handler(), http.MethodPost, "/api/v1/agents/register", body)
	assert.Equal(t, http.StatusConflict, rec.Code)
	assert.Contains(t, rec.Body.String(), "agent already registered")
}

func TestRegisterAgentWrongContentType(t *testing.T) {
	srv, _ := testServer(t)
	req := httptest.NewRequest(http.MethodPost, "/api/v1/agents/register", strings.NewReader(`{}`))
	req.Header.Set("Content-Type", "text/plain")
	rec := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, req)
	assert.Equal(t, http.StatusBadRequest, rec.Code)
	assert.Contains(t, rec.Body.String(), "Content-Type must be application/json")
}

func TestRegisterAgentUnknownField(t *testing.T) {
	srv, _ := testServer(t)
	body := []byte(`{"id": "test-agent", "address": "localhost:50051", "unknown_field": "bad"}`)
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/agents/register", body)
	assert.Equal(t, http.StatusBadRequest, rec.Code)
	assert.Contains(t, rec.Body.String(), "invalid or malformed JSON body")
}

// TestRegisterAgentWithName verifies that a registration payload containing
// "name" (as sent by the Python agent server) is accepted by the strict JSON
// decoder (DisallowUnknownFields).  Before the Name field was added to
// registerAgentRequest, this payload was rejected as an unknown field.
// (PR #71 review finding — missing coverage for the original bug.)
func TestRegisterAgentWithName(t *testing.T) {
	srv, _ := testServer(t)
	body := []byte(`{"id": "code-writer", "name": "Code Writer", "address": "localhost:50051", "capabilities": ["code_generation"]}`)
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/agents/register", body)
	assert.Equal(t, http.StatusCreated, rec.Code)

	var resp agentResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	assert.Equal(t, "code-writer", resp.ID)
	assert.Equal(t, "Code Writer", resp.Name)
	assert.Equal(t, "localhost:50051", resp.Address)
	assert.Equal(t, []string{"code_generation"}, resp.Capabilities)
}

func TestRegisterAgentNilCapabilities(t *testing.T) {
	srv, _ := testServer(t)
	body := []byte(`{"id": "test-agent", "address": "localhost:50051"}`)
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/agents/register", body)
	require.Equal(t, http.StatusCreated, rec.Code)

	var resp agentResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	// Capabilities must serialize as [] not null
	assert.NotNil(t, resp.Capabilities)
	assert.Empty(t, resp.Capabilities)
	// Verify raw JSON contains [] not null
	assert.Contains(t, rec.Body.String(), `"capabilities":[]`)
}

// --- List Agents ---

func TestListAgentsEmpty(t *testing.T) {
	srv, _ := testServer(t)
	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/agents", nil)
	assert.Equal(t, http.StatusOK, rec.Code)
	assert.Equal(t, "[]\n", rec.Body.String())
}

func TestListAgentsWithRegistrations(t *testing.T) {
	srv, _ := testServer(t)
	h := srv.Handler()

	// Register two agents
	body1 := []byte(`{"id": "agent-01", "address": "localhost:50051", "capabilities": ["coding"]}`)
	rec := doRequest(h, http.MethodPost, "/api/v1/agents/register", body1)
	require.Equal(t, http.StatusCreated, rec.Code)

	body2 := []byte(`{"id": "agent-02", "address": "localhost:50052", "capabilities": ["review"]}`)
	rec = doRequest(h, http.MethodPost, "/api/v1/agents/register", body2)
	require.Equal(t, http.StatusCreated, rec.Code)

	// List
	rec = doRequest(h, http.MethodGet, "/api/v1/agents", nil)
	assert.Equal(t, http.StatusOK, rec.Code)

	var list []agentResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &list))
	assert.Len(t, list, 2)
}

// --- Get Agent ---

func TestGetAgent(t *testing.T) {
	srv, _ := testServer(t)
	h := srv.Handler()

	body := []byte(`{"id": "code-writer", "address": "localhost:50051", "capabilities": ["coding"]}`)
	rec := doRequest(h, http.MethodPost, "/api/v1/agents/register", body)
	require.Equal(t, http.StatusCreated, rec.Code)

	rec = doRequest(h, http.MethodGet, "/api/v1/agents/code-writer", nil)
	assert.Equal(t, http.StatusOK, rec.Code)

	var resp agentResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	assert.Equal(t, "code-writer", resp.ID)
	assert.Equal(t, "localhost:50051", resp.Address)
	assert.Equal(t, "healthy", resp.Status)
}

func TestGetAgentNotFound(t *testing.T) {
	srv, _ := testServer(t)
	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/agents/nonexistent-agent", nil)
	assert.Equal(t, http.StatusNotFound, rec.Code)
	assert.Contains(t, rec.Body.String(), "agent not found")
}

// --- Delete Agent ---

func TestDeleteAgent(t *testing.T) {
	srv, _ := testServer(t)
	h := srv.Handler()

	body := []byte(`{"id": "code-writer", "address": "localhost:50051"}`)
	rec := doRequest(h, http.MethodPost, "/api/v1/agents/register", body)
	require.Equal(t, http.StatusCreated, rec.Code)

	rec = doRequest(h, http.MethodDelete, "/api/v1/agents/code-writer", nil)
	assert.Equal(t, http.StatusNoContent, rec.Code)

	// Verify gone
	rec = doRequest(h, http.MethodGet, "/api/v1/agents/code-writer", nil)
	assert.Equal(t, http.StatusNotFound, rec.Code)
}

func TestDeleteAgentNotFound(t *testing.T) {
	srv, _ := testServer(t)
	rec := doRequest(srv.Handler(), http.MethodDelete, "/api/v1/agents/nonexistent-agent", nil)
	assert.Equal(t, http.StatusNotFound, rec.Code)
	assert.Contains(t, rec.Body.String(), "agent not found")
}

// --- Re-registration ---

func TestAgentReRegistration(t *testing.T) {
	srv, _ := testServer(t)
	h := srv.Handler()

	body := []byte(`{"id": "code-writer", "address": "localhost:50051"}`)
	rec := doRequest(h, http.MethodPost, "/api/v1/agents/register", body)
	require.Equal(t, http.StatusCreated, rec.Code)

	// Delete
	rec = doRequest(h, http.MethodDelete, "/api/v1/agents/code-writer", nil)
	assert.Equal(t, http.StatusNoContent, rec.Code)

	// Re-register with same ID
	rec = doRequest(h, http.MethodPost, "/api/v1/agents/register", body)
	assert.Equal(t, http.StatusCreated, rec.Code)
}

// --- Agent Lifecycle ---

func TestAgentLifecycle(t *testing.T) {
	srv, _ := testServer(t)
	h := srv.Handler()

	// 1. Register → 201
	body := []byte(`{"id": "lifecycle-agent", "address": "localhost:50051", "capabilities": ["cap-a"]}`)
	rec := doRequest(h, http.MethodPost, "/api/v1/agents/register", body)
	require.Equal(t, http.StatusCreated, rec.Code)

	// 2. Get → 200
	rec = doRequest(h, http.MethodGet, "/api/v1/agents/lifecycle-agent", nil)
	assert.Equal(t, http.StatusOK, rec.Code)
	var getResp agentResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &getResp))
	assert.Equal(t, "lifecycle-agent", getResp.ID)
	assert.Equal(t, "healthy", getResp.Status)

	// 3. List → contains the agent
	rec = doRequest(h, http.MethodGet, "/api/v1/agents", nil)
	assert.Equal(t, http.StatusOK, rec.Code)
	var list []agentResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &list))
	assert.Len(t, list, 1)

	// 4. Delete → 204
	rec = doRequest(h, http.MethodDelete, "/api/v1/agents/lifecycle-agent", nil)
	assert.Equal(t, http.StatusNoContent, rec.Code)

	// 5. Get again → 404
	rec = doRequest(h, http.MethodGet, "/api/v1/agents/lifecycle-agent", nil)
	assert.Equal(t, http.StatusNotFound, rec.Code)

	// 6. List → empty
	rec = doRequest(h, http.MethodGet, "/api/v1/agents", nil)
	assert.Equal(t, http.StatusOK, rec.Code)
	assert.Equal(t, "[]\n", rec.Body.String())
}

// --- agentStatusString ---

func TestAgentStatusString(t *testing.T) {
	tests := []struct {
		status registry.AgentStatus
		want   string
	}{
		{registry.StatusHealthy, "healthy"},
		{registry.StatusDegraded, "degraded"},
		{registry.StatusOffline, "offline"},
		{registry.StatusUnknown, "unknown"},
		{registry.AgentStatus(99), "unknown"},
	}
	for _, tc := range tests {
		assert.Equal(t, tc.want, agentStatusString(tc.status))
	}
}

// --- Concurrent Agent Access ---

func TestConcurrentAgentAccess(t *testing.T) {
	srv, _ := testServer(t)
	h := srv.Handler()

	done := make(chan struct{})
	for i := 0; i < 20; i++ {
		go func(n int) {
			defer func() { done <- struct{}{} }()
			id := fmt.Sprintf("agent-%02d", n)
			body, _ := json.Marshal(registerAgentRequest{ID: id, Address: "localhost:50051"})
			rec := doRequest(h, http.MethodPost, "/api/v1/agents/register", body)
			assert.Equal(t, http.StatusCreated, rec.Code)
		}(i)
	}
	for i := 0; i < 20; i++ {
		<-done
	}

	// Verify all 20 agents exist
	rec := doRequest(h, http.MethodGet, "/api/v1/agents", nil)
	var list []agentResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &list))
	assert.Len(t, list, 20)
}

// --- Agent ID Validation on GET/DELETE (Review finding F-01) ---

func TestGetAgentInvalidID(t *testing.T) {
	srv, _ := testServer(t)
	tests := []struct {
		name string
		id   string
	}{
		{"uppercase", "Code-Writer"},
		{"underscore", "code_writer"},
		{"single char", "a"},
		{"starts with dash", "-writer"},
		{"ends with dash", "writer-"},
		{"with dots", "code.writer"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/agents/"+tc.id, nil)
			assert.Equal(t, http.StatusBadRequest, rec.Code)
			assert.Contains(t, rec.Body.String(), "invalid agent ID format")
		})
	}
}

func TestDeleteAgentInvalidID(t *testing.T) {
	srv, _ := testServer(t)
	tests := []struct {
		name string
		id   string
	}{
		{"uppercase", "Code-Writer"},
		{"underscore", "code_writer"},
		{"single char", "a"},
		{"starts with dash", "-writer"},
		{"ends with dash", "writer-"},
		{"with dots", "code.writer"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			rec := doRequest(srv.Handler(), http.MethodDelete, "/api/v1/agents/"+tc.id, nil)
			assert.Equal(t, http.StatusBadRequest, rec.Code)
			assert.Contains(t, rec.Body.String(), "invalid agent ID format")
		})
	}
}

// --- Agent Registration Edge Cases (Review findings F-06, F-08) ---

func TestRegisterAgentEmptyBody(t *testing.T) {
	srv, _ := testServer(t)
	req := httptest.NewRequest(http.MethodPost, "/api/v1/agents/register", strings.NewReader(""))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, req)
	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

func TestRegisterAgentBodyTooLarge(t *testing.T) {
	srv, _ := testServer(t)
	bigValue := strings.Repeat("x", (1<<20)+100)
	body := []byte(`{"id":"` + bigValue + `"}`)
	req := httptest.NewRequest(http.MethodPost, "/api/v1/agents/register", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, req)
	assert.Equal(t, http.StatusBadRequest, rec.Code)
	assert.Contains(t, rec.Body.String(), "too large")
}

// --- Failing Registry (Review finding F-05, carry-forward from PR #14 F-01) ---

// failingRegistry wraps a real Registry and forces specific methods to return
// non-sentinel errors, enabling 500 error-path coverage for agent handlers.
type failingRegistry struct {
	registry.Registry
	failOn string // method name to fail on
}

func (f *failingRegistry) Register(ctx context.Context, agent registry.AgentInfo) error {
	if f.failOn == "Register" {
		return errors.New("simulated db error")
	}
	return f.Registry.Register(ctx, agent)
}

func (f *failingRegistry) List(ctx context.Context) ([]registry.AgentInfo, error) {
	if f.failOn == "List" {
		return nil, errors.New("simulated db error")
	}
	return f.Registry.List(ctx)
}

func (f *failingRegistry) Get(ctx context.Context, agentID string) (*registry.AgentInfo, error) {
	if f.failOn == "Get" {
		return nil, errors.New("simulated db error")
	}
	return f.Registry.Get(ctx, agentID)
}

func (f *failingRegistry) Unregister(ctx context.Context, agentID string) error {
	if f.failOn == "Unregister" {
		return errors.New("simulated db error")
	}
	return f.Registry.Unregister(ctx, agentID)
}

// testServerWithRegistry creates a Server using the provided registry instead of
// the default InMemoryRegistry. Used by failingRegistry tests.
func testServerWithRegistry(t *testing.T, reg registry.Registry) *Server {
	t.Helper()
	dir := t.TempDir()
	logger := zap.NewNop()
	store := state.NewInMemoryStore(logger)
	pl := planner.NewYAMLPlanner(logger)
	srv, err := New("127.0.0.1:0", dir, store, reg, pl, logger)
	require.NoError(t, err)
	return srv
}

func TestRegisterAgentInternalError(t *testing.T) {
	reg := &failingRegistry{
		Registry: registry.NewInMemoryRegistry(zap.NewNop()),
		failOn:   "Register",
	}
	srv := testServerWithRegistry(t, reg)
	body := []byte(`{"id": "test-agent", "address": "localhost:50051"}`)
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/agents/register", body)
	assert.Equal(t, http.StatusInternalServerError, rec.Code)
	assert.Contains(t, rec.Body.String(), "INTERNAL")
}

func TestListAgentsInternalError(t *testing.T) {
	reg := &failingRegistry{
		Registry: registry.NewInMemoryRegistry(zap.NewNop()),
		failOn:   "List",
	}
	srv := testServerWithRegistry(t, reg)
	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/agents", nil)
	assert.Equal(t, http.StatusInternalServerError, rec.Code)
	assert.Contains(t, rec.Body.String(), "INTERNAL")
}

func TestGetAgentInternalError(t *testing.T) {
	reg := &failingRegistry{
		Registry: registry.NewInMemoryRegistry(zap.NewNop()),
		failOn:   "Get",
	}
	srv := testServerWithRegistry(t, reg)
	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/agents/test-agent", nil)
	assert.Equal(t, http.StatusInternalServerError, rec.Code)
	assert.Contains(t, rec.Body.String(), "INTERNAL")
}

func TestDeleteAgentInternalError(t *testing.T) {
	reg := &failingRegistry{
		Registry: registry.NewInMemoryRegistry(zap.NewNop()),
		failOn:   "Unregister",
	}
	srv := testServerWithRegistry(t, reg)
	rec := doRequest(srv.Handler(), http.MethodDelete, "/api/v1/agents/test-agent", nil)
	assert.Equal(t, http.StatusInternalServerError, rec.Code)
	assert.Contains(t, rec.Body.String(), "INTERNAL")
}

// =============================================================================
// Stub Endpoint Tests (Phase 3)
// =============================================================================

func TestGetLogsStub(t *testing.T) {
	srv, _ := testServer(t)
	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/executions/any-id/logs", nil)
	assert.Equal(t, http.StatusNotImplemented, rec.Code)
	assert.Contains(t, rec.Body.String(), "NOT_IMPLEMENTED")
	assert.Contains(t, rec.Body.String(), "not implemented in v0.1")
}

func TestGetCostSummaryStub(t *testing.T) {
	srv, _ := testServer(t)
	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/cost/summary", nil)
	assert.Equal(t, http.StatusNotImplemented, rec.Code)
	assert.Contains(t, rec.Body.String(), "NOT_IMPLEMENTED")
	assert.Contains(t, rec.Body.String(), "not implemented in v0.1")
}

// NOTE(review-F09): Wrong-method tests document the HTTP method contract for
// stub endpoints. Go 1.22+ ServeMux pattern routing handles 405 automatically.

func TestLogsStubWrongMethod(t *testing.T) {
	srv, _ := testServer(t)
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/executions/any-id/logs", nil)
	assert.Equal(t, http.StatusMethodNotAllowed, rec.Code)
}

func TestCostSummaryStubWrongMethod(t *testing.T) {
	srv, _ := testServer(t)
	rec := doRequest(srv.Handler(), http.MethodDelete, "/api/v1/cost/summary", nil)
	assert.Equal(t, http.StatusMethodNotAllowed, rec.Code)
}

// =============================================================================
// Failing Store Tests (PR #14 carry-forward F-01)
// =============================================================================

// failingStore wraps a real Store and forces specific methods to return
// non-sentinel errors, enabling 500 error-path coverage for workflow handlers.
type failingStore struct {
	state.Store
	failOn string // method name to fail on
}

func (f *failingStore) GetRun(ctx context.Context, runID string) (*state.WorkflowRun, error) {
	if f.failOn == "GetRun" {
		return nil, errors.New("simulated db error")
	}
	return f.Store.GetRun(ctx, runID)
}

func (f *failingStore) ListRuns(ctx context.Context) ([]*state.WorkflowRun, error) {
	if f.failOn == "ListRuns" {
		return nil, errors.New("simulated db error")
	}
	return f.Store.ListRuns(ctx)
}

func (f *failingStore) DeleteRun(ctx context.Context, runID string) error {
	if f.failOn == "DeleteRun" {
		return errors.New("simulated db error")
	}
	return f.Store.DeleteRun(ctx, runID)
}

func (f *failingStore) CreateRun(ctx context.Context, run *state.WorkflowRun) error {
	if f.failOn == "CreateRun" {
		return errors.New("simulated db error")
	}
	return f.Store.CreateRun(ctx, run)
}

// NOTE(review-F06): UpdateRunStatus and UpdateStepState are not called by any
// v0.1 handler, but without explicit stubs the embedded interface's nil method
// values would panic at runtime if RFC 0003 Scheduler/Executor handlers call
// them before the stubs are replaced with real implementations.

func (f *failingStore) UpdateRunStatus(ctx context.Context, runID string, status state.RunStatus) error {
	if f.failOn == "UpdateRunStatus" {
		return errors.New("simulated db error")
	}
	return f.Store.UpdateRunStatus(ctx, runID, status)
}

func (f *failingStore) UpdateStepState(ctx context.Context, runID string, step state.StepState) error {
	if f.failOn == "UpdateStepState" {
		return errors.New("simulated db error")
	}
	return f.Store.UpdateStepState(ctx, runID, step)
}

// testServerWithStore creates a Server using the provided store instead of
// the default InMemoryStore. Used by failingStore tests.
func testServerWithStore(t *testing.T, store state.Store) (*Server, string) {
	t.Helper()
	dir := t.TempDir()
	logger := zap.NewNop()
	reg := registry.NewInMemoryRegistry(logger)
	pl := planner.NewYAMLPlanner(logger)
	srv, err := New("127.0.0.1:0", dir, store, reg, pl, logger)
	require.NoError(t, err)
	return srv, dir
}

func TestGetWorkflowStatusInternalError(t *testing.T) {
	store := &failingStore{
		Store:  state.NewInMemoryStore(zap.NewNop()),
		failOn: "GetRun",
	}
	srv, _ := testServerWithStore(t, store)
	// Use a valid UUID format to pass validation
	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/workflows/00000000-0000-4000-8000-000000000001/status", nil)
	assert.Equal(t, http.StatusInternalServerError, rec.Code)
	assert.Contains(t, rec.Body.String(), "INTERNAL")
}

func TestListWorkflowsInternalError(t *testing.T) {
	store := &failingStore{
		Store:  state.NewInMemoryStore(zap.NewNop()),
		failOn: "ListRuns",
	}
	srv, _ := testServerWithStore(t, store)
	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/workflows", nil)
	assert.Equal(t, http.StatusInternalServerError, rec.Code)
	assert.Contains(t, rec.Body.String(), "INTERNAL")
}

func TestDeleteWorkflowInternalError(t *testing.T) {
	store := &failingStore{
		Store:  state.NewInMemoryStore(zap.NewNop()),
		failOn: "DeleteRun",
	}
	srv, dir := testServerWithStore(t, store)
	writeWorkflowFixture(t, dir, "test-wf")
	h := srv.Handler()

	// Create a run first (CreateRun not failing)
	body, _ := json.Marshal(submitWorkflowRunRequest{WorkflowID: "test-wf"})
	rec := doRequest(h, http.MethodPost, "/api/v1/workflows/run", body)
	require.Equal(t, http.StatusCreated, rec.Code)
	var cr submitWorkflowRunResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &cr))

	// Delete should hit the 500 path
	rec = doRequest(h, http.MethodDelete, "/api/v1/workflows/"+cr.RunID, nil)
	assert.Equal(t, http.StatusInternalServerError, rec.Code)
	assert.Contains(t, rec.Body.String(), "INTERNAL")
}

func TestDeleteWorkflowGetRunInternalError(t *testing.T) {
	store := &failingStore{
		Store:  state.NewInMemoryStore(zap.NewNop()),
		failOn: "GetRun",
	}
	srv, _ := testServerWithStore(t, store)
	// Use a valid UUID to pass validation — GetRun will fail with 500
	rec := doRequest(srv.Handler(), http.MethodDelete, "/api/v1/workflows/00000000-0000-4000-8000-000000000001", nil)
	assert.Equal(t, http.StatusInternalServerError, rec.Code)
	assert.Contains(t, rec.Body.String(), "INTERNAL")
}

func TestSubmitWorkflowRunCreateRunInternalError(t *testing.T) {
	store := &failingStore{
		Store:  state.NewInMemoryStore(zap.NewNop()),
		failOn: "CreateRun",
	}
	srv, dir := testServerWithStore(t, store)
	writeWorkflowFixture(t, dir, "test-wf")
	body, _ := json.Marshal(submitWorkflowRunRequest{WorkflowID: "test-wf"})
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/workflows/run", body)
	assert.Equal(t, http.StatusInternalServerError, rec.Code)
	assert.Contains(t, rec.Body.String(), "INTERNAL")
}

// =============================================================================
// Mixed Concurrent Stress Tests (PR #14 carry-forward F-02, PR #16 F-05)
// =============================================================================

func TestMixedConcurrentWorkflowAccess(t *testing.T) {
	srv, dir := testServer(t)
	writeWorkflowFixture(t, dir, "concurrent-wf")
	h := srv.Handler()

	// Pre-create some runs to read and delete
	var runIDs []string
	for i := 0; i < 10; i++ {
		body, _ := json.Marshal(submitWorkflowRunRequest{WorkflowID: "concurrent-wf"})
		rec := doRequest(h, http.MethodPost, "/api/v1/workflows/run", body)
		require.Equal(t, http.StatusCreated, rec.Code)
		var cr submitWorkflowRunResponse
		require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &cr))
		runIDs = append(runIDs, cr.RunID)
	}

	// NOTE(review-F04): goroutines assert status codes to verify correctness,
	// not just absence of panics/deadlocks. Failures are collected via t.Errorf
	// which is goroutine-safe.
	done := make(chan struct{}, 35)

	// 10 goroutines submit new runs
	for i := 0; i < 10; i++ {
		go func() {
			defer func() { done <- struct{}{} }()
			body, _ := json.Marshal(submitWorkflowRunRequest{WorkflowID: "concurrent-wf"})
			rec := doRequest(h, http.MethodPost, "/api/v1/workflows/run", body)
			if rec.Code != http.StatusCreated {
				t.Errorf("concurrent submit: got %d, want %d", rec.Code, http.StatusCreated)
			}
		}()
	}

	// 5 goroutines read runs not targeted by DELETE (safe — always 200)
	for i := 5; i < 10; i++ {
		go func(id string) {
			defer func() { done <- struct{}{} }()
			rec := doRequest(h, http.MethodGet, "/api/v1/workflows/"+id+"/status", nil)
			if rec.Code != http.StatusOK {
				t.Errorf("concurrent get %s: got %d, want %d", id, rec.Code, http.StatusOK)
			}
		}(runIDs[i])
	}

	// 5 goroutines read runs that may be concurrently deleted (accept 200 or 404)
	for i := 0; i < 5; i++ {
		go func(id string) {
			defer func() { done <- struct{}{} }()
			rec := doRequest(h, http.MethodGet, "/api/v1/workflows/"+id+"/status", nil)
			if rec.Code != http.StatusOK && rec.Code != http.StatusNotFound {
				t.Errorf("concurrent get-or-miss %s: got %d, want 200 or 404", id, rec.Code)
			}
		}(runIDs[i])
	}

	// 10 goroutines list all runs
	for i := 0; i < 10; i++ {
		go func() {
			defer func() { done <- struct{}{} }()
			rec := doRequest(h, http.MethodGet, "/api/v1/workflows", nil)
			if rec.Code != http.StatusOK {
				t.Errorf("concurrent list: got %d, want %d", rec.Code, http.StatusOK)
			}
		}()
	}

	// 5 goroutines delete pre-created runs (PR #17 F-04: exercises TOCTOU delete path)
	for i := 0; i < 5; i++ {
		go func(id string) {
			defer func() { done <- struct{}{} }()
			rec := doRequest(h, http.MethodDelete, "/api/v1/workflows/"+id, nil)
			// 204 = deleted, 404 = already deleted by another goroutine — both valid
			if rec.Code != http.StatusNoContent && rec.Code != http.StatusNotFound {
				t.Errorf("concurrent delete %s: got %d, want 204 or 404", id, rec.Code)
			}
		}(runIDs[i])
	}

	for i := 0; i < 35; i++ {
		<-done
	}
}

func TestMixedConcurrentAgentAccess(t *testing.T) {
	srv, _ := testServer(t)
	h := srv.Handler()

	// Pre-register some agents
	for i := 0; i < 10; i++ {
		id := fmt.Sprintf("pre-agent-%02d", i)
		body, _ := json.Marshal(registerAgentRequest{ID: id, Address: "localhost:50051"})
		rec := doRequest(h, http.MethodPost, "/api/v1/agents/register", body)
		require.Equal(t, http.StatusCreated, rec.Code)
	}

	// NOTE(review-F04): goroutines assert status codes to verify correctness,
	// not just absence of panics/deadlocks.
	done := make(chan struct{}, 35)

	// 10 goroutines register new agents
	for i := 0; i < 10; i++ {
		go func(n int) {
			defer func() { done <- struct{}{} }()
			id := fmt.Sprintf("new-agent-%02d", n)
			body, _ := json.Marshal(registerAgentRequest{ID: id, Address: "localhost:50052"})
			rec := doRequest(h, http.MethodPost, "/api/v1/agents/register", body)
			if rec.Code != http.StatusCreated {
				t.Errorf("concurrent register %s: got %d, want %d", id, rec.Code, http.StatusCreated)
			}
		}(i)
	}

	// 5 goroutines get agents not targeted by DELETE (safe — always 200)
	for i := 5; i < 10; i++ {
		go func(n int) {
			defer func() { done <- struct{}{} }()
			id := fmt.Sprintf("pre-agent-%02d", n)
			rec := doRequest(h, http.MethodGet, "/api/v1/agents/"+id, nil)
			if rec.Code != http.StatusOK {
				t.Errorf("concurrent get %s: got %d, want %d", id, rec.Code, http.StatusOK)
			}
		}(i)
	}

	// 5 goroutines get agents that may be concurrently deleted (accept 200 or 404)
	for i := 0; i < 5; i++ {
		go func(n int) {
			defer func() { done <- struct{}{} }()
			id := fmt.Sprintf("pre-agent-%02d", n)
			rec := doRequest(h, http.MethodGet, "/api/v1/agents/"+id, nil)
			if rec.Code != http.StatusOK && rec.Code != http.StatusNotFound {
				t.Errorf("concurrent get-or-miss %s: got %d, want 200 or 404", id, rec.Code)
			}
		}(i)
	}

	// 10 goroutines list all agents
	for i := 0; i < 10; i++ {
		go func() {
			defer func() { done <- struct{}{} }()
			rec := doRequest(h, http.MethodGet, "/api/v1/agents", nil)
			if rec.Code != http.StatusOK {
				t.Errorf("concurrent list: got %d, want %d", rec.Code, http.StatusOK)
			}
		}()
	}

	// 5 goroutines delete pre-registered agents (PR #17 F-04: exercises concurrent unregister)
	for i := 0; i < 5; i++ {
		go func(n int) {
			defer func() { done <- struct{}{} }()
			id := fmt.Sprintf("pre-agent-%02d", n)
			rec := doRequest(h, http.MethodDelete, "/api/v1/agents/"+id, nil)
			// 204 = deleted, 404 = already deleted by another goroutine — both valid
			if rec.Code != http.StatusNoContent && rec.Code != http.StatusNotFound {
				t.Errorf("concurrent delete %s: got %d, want 204 or 404", id, rec.Code)
			}
		}(i)
	}

	for i := 0; i < 35; i++ {
		<-done
	}
}

// =============================================================================
// PR #16 Carry-Forward Tests
// =============================================================================

// TestRegisterAgentContentTypeWithCharset validates that charset=utf-8 parameter
// is accepted (parity with TestSubmitWorkflowRunContentTypeWithCharset).
// (PR #16 carry-forward F-03)
func TestRegisterAgentContentTypeWithCharset(t *testing.T) {
	srv, _ := testServer(t)
	body := []byte(`{"id": "test-agent", "address": "localhost:50051"}`)
	req := httptest.NewRequest(http.MethodPost, "/api/v1/agents/register", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json; charset=utf-8")
	rec := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, req)
	assert.Equal(t, http.StatusCreated, rec.Code)
}

// TestListAgentsWithRegistrationsSetBased uses set-based ID assertion
// instead of just checking count. (PR #16 carry-forward F-02)
func TestListAgentsWithRegistrationsSetBased(t *testing.T) {
	srv, _ := testServer(t)
	h := srv.Handler()

	body1 := []byte(`{"id": "agent-aa", "address": "localhost:50051", "capabilities": ["coding"]}`)
	rec := doRequest(h, http.MethodPost, "/api/v1/agents/register", body1)
	require.Equal(t, http.StatusCreated, rec.Code)

	body2 := []byte(`{"id": "agent-bb", "address": "localhost:50052", "capabilities": ["review"]}`)
	rec = doRequest(h, http.MethodPost, "/api/v1/agents/register", body2)
	require.Equal(t, http.StatusCreated, rec.Code)

	rec = doRequest(h, http.MethodGet, "/api/v1/agents", nil)
	assert.Equal(t, http.StatusOK, rec.Code)

	var list []agentResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &list))
	require.Len(t, list, 2)

	ids := map[string]bool{}
	for _, a := range list {
		ids[a.ID] = true
	}
	assert.True(t, ids["agent-aa"], "expected agent-aa in list")
	assert.True(t, ids["agent-bb"], "expected agent-bb in list")
}

// TestRegisterAgentAddressTooLong validates max-length enforcement.
// (PR #16 carry-forward F-01)
func TestRegisterAgentAddressTooLong(t *testing.T) {
	srv, _ := testServer(t)
	longAddr := strings.Repeat("a", 254)
	body, _ := json.Marshal(registerAgentRequest{ID: "test-agent", Address: longAddr})
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/agents/register", body)
	assert.Equal(t, http.StatusBadRequest, rec.Code)
	assert.Contains(t, rec.Body.String(), "exceeds maximum length")
}

func TestRegisterAgentAddressMaxLength(t *testing.T) {
	srv, _ := testServer(t)
	// 253 characters should be accepted
	maxAddr := strings.Repeat("a", 253)
	body, _ := json.Marshal(registerAgentRequest{ID: "test-agent", Address: maxAddr})
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/agents/register", body)
	assert.Equal(t, http.StatusCreated, rec.Code)
}

// TestRegisterAgentNameTooLong validates max-length enforcement for the
// display name field.  (PR #71 deep-review §2.3)
func TestRegisterAgentNameTooLong(t *testing.T) {
	srv, _ := testServer(t)
	longName := strings.Repeat("A", 101)
	body, _ := json.Marshal(registerAgentRequest{ID: "test-agent", Name: longName, Address: "localhost:50051"})
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/agents/register", body)
	assert.Equal(t, http.StatusBadRequest, rec.Code)
	assert.Contains(t, rec.Body.String(), "name exceeds maximum length")
}

func TestRegisterAgentNameMaxLength(t *testing.T) {
	srv, _ := testServer(t)
	// 100 characters should be accepted
	maxName := strings.Repeat("A", 100)
	body, _ := json.Marshal(registerAgentRequest{ID: "test-agent", Name: maxName, Address: "localhost:50051"})
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/agents/register", body)
	assert.Equal(t, http.StatusCreated, rec.Code)

	var resp agentResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	assert.Equal(t, maxName, resp.Name)
}
