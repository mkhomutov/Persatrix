package server

import (
	"context"
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/planner"
	"github.com/mkhomutov/persatrix/internal/registry"
	"github.com/mkhomutov/persatrix/internal/state"
)

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

func TestDeleteWorkflowInvalidRunID(t *testing.T) {
	srv, _ := testServer(t)
	// Review finding F-02: previous test used /api/v1/workflows/not-a-uuid/status
	// which hit the GET status route with wrong method (405), not the DELETE handler.
	rec := doRequest(srv.Handler(), http.MethodDelete, "/api/v1/workflows/not-a-uuid", nil)
	assert.Equal(t, http.StatusBadRequest, rec.Code,
		"non-UUID IDs should be rejected with 400 before reaching the store")
}

func TestDeleteWorkflowValidUUIDNotFound(t *testing.T) {
	srv, _ := testServer(t)
	rec := doRequest(srv.Handler(), http.MethodDelete, "/api/v1/workflows/00000000-0000-0000-0000-000000000000", nil)
	assert.Equal(t, http.StatusNotFound, rec.Code)
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

// --- resolveWorkflowPath security ---

func TestResolveWorkflowPathPrefixCheck(t *testing.T) {
	srv, _ := testServer(t)
	// Direct call to resolveWorkflowPath with a valid-format but nonexistent ID
	_, err := srv.resolveWorkflowPath("nonexistent-wf")
	assert.ErrorIs(t, err, ErrWorkflowNotFound)
}

// TestResolveWorkflowPathSymlinkEscape validates the symlink escape defense.
// (Review finding T-01): The RFC explicitly requires testing a symlink pointing
// outside the workflows directory. This validates the most critical security
// property of the 3-layer path traversal defense (regex + EvalSymlinks + HasPrefix).
func TestResolveWorkflowPathSymlinkEscape(t *testing.T) {
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
