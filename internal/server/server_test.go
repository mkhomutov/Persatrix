package server

import (
	"bytes"
	"context"
	"encoding/json"
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

	"github.com/orchestr8/orchestr8/internal/planner"
	"github.com/orchestr8/orchestr8/internal/registry"
	"github.com/orchestr8/orchestr8/internal/state"
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
	assert.Equal(t, http.StatusNotFound, rec.Code)
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
	rec := doRequest(srv.Handler(), http.MethodDelete, "/api/v1/workflows/nonexistent-id", nil)
	assert.Equal(t, http.StatusNotFound, rec.Code)
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

	// Give server a moment to start, then cancel
	time.Sleep(50 * time.Millisecond)
	cancel()

	err := <-errCh
	assert.NoError(t, err)
}

// --- DeleteWorkflow: delete completed/failed/cancelled statuses ---

func TestDeleteNonRunningStatuses(t *testing.T) {
	statuses := []state.RunStatus{state.RunCompleted, state.RunFailed, state.RunCancelled}
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
