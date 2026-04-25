package server

import (
	"context"
	"encoding/json"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/mkhomutov/persatrix/internal/state"
)

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

// TestGetWorkflowStatusValidUUIDNotFound validates that a valid UUID that doesn't
// exist in the store returns 404 (not 400).
func TestGetWorkflowStatusValidUUIDNotFound(t *testing.T) {
	srv, _ := testServer(t)
	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/workflows/00000000-0000-0000-0000-000000000000/status", nil)
	assert.Equal(t, http.StatusNotFound, rec.Code)
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

// --- StepExecutionMetadata in API response (RFC 0006 PR 4a) ---

func TestGetWorkflowStatus_StepMetadata(t *testing.T) {
	srv, dir := testServer(t)
	writeWorkflowFixture(t, dir, "test-wf")

	// Submit a run.
	body, _ := json.Marshal(submitWorkflowRunRequest{WorkflowID: "test-wf"})
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/workflows/run", body)
	require.Equal(t, http.StatusCreated, rec.Code)
	var createResp submitWorkflowRunResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &createResp))

	// Manually update step state with metadata via the store.
	meta := &state.StepExecutionMetadata{
		TokensUsed:       1200,
		LLMCallCount:     2,
		RetryCount:       1,
		CacheHit:         false,
		WallTimeMs:       3500,
		EstimatedCostUSD: 0.012,
	}
	err := srv.store.UpdateStepState(context.Background(), createResp.RunID, state.StepState{
		StepID:   "design",
		Status:   state.RunCompleted,
		Output:   "design output",
		Metadata: meta,
	})
	require.NoError(t, err)

	// Get status and verify metadata appears in response.
	rec = doRequest(srv.Handler(), http.MethodGet, "/api/v1/workflows/"+createResp.RunID+"/status", nil)
	assert.Equal(t, http.StatusOK, rec.Code)

	var raw map[string]json.RawMessage
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &raw))

	var steps map[string]json.RawMessage
	require.NoError(t, json.Unmarshal(raw["steps"], &steps))

	var stepData map[string]json.RawMessage
	require.NoError(t, json.Unmarshal(steps["design"], &stepData))

	// Verify step fields.
	assert.JSONEq(t, `"completed"`, string(stepData["status"]))
	assert.JSONEq(t, `"design output"`, string(stepData["output"]))

	// Verify metadata.
	var gotMeta state.StepExecutionMetadata
	require.NoError(t, json.Unmarshal(stepData["metadata"], &gotMeta))
	assert.Equal(t, 1200, gotMeta.TokensUsed)
	assert.Equal(t, 2, gotMeta.LLMCallCount)
	assert.Equal(t, 1, gotMeta.RetryCount)
	assert.False(t, gotMeta.CacheHit)
	assert.Equal(t, int64(3500), gotMeta.WallTimeMs)
	assert.InDelta(t, 0.012, gotMeta.EstimatedCostUSD, 1e-9)
}

func TestGetWorkflowStatus_StepWithoutMetadata(t *testing.T) {
	srv, dir := testServer(t)
	writeWorkflowFixture(t, dir, "test-wf")

	body, _ := json.Marshal(submitWorkflowRunRequest{WorkflowID: "test-wf"})
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/workflows/run", body)
	require.Equal(t, http.StatusCreated, rec.Code)
	var createResp submitWorkflowRunResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &createResp))

	// Step without metadata (pre-PR 4a behavior).
	err := srv.store.UpdateStepState(context.Background(), createResp.RunID, state.StepState{
		StepID: "design",
		Status: state.RunRunning,
	})
	require.NoError(t, err)

	rec = doRequest(srv.Handler(), http.MethodGet, "/api/v1/workflows/"+createResp.RunID+"/status", nil)
	assert.Equal(t, http.StatusOK, rec.Code)

	var raw map[string]json.RawMessage
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &raw))

	var steps map[string]json.RawMessage
	require.NoError(t, json.Unmarshal(raw["steps"], &steps))

	var stepData map[string]json.RawMessage
	require.NoError(t, json.Unmarshal(steps["design"], &stepData))

	assert.JSONEq(t, `"running"`, string(stepData["status"]))
	// Metadata should not be present when nil.
	_, hasMetadata := stepData["metadata"]
	assert.False(t, hasMetadata, "metadata should not appear in response when nil")
}
