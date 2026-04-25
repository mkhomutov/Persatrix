package server

import (
	"encoding/json"
	"net/http"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/registry"
	"github.com/mkhomutov/persatrix/internal/state"
)

// =============================================================================
// Workflow Handler Internal Error Tests (PR #14 carry-forward F-01)
// Uses failingStore from server_helpers_test.go.
// =============================================================================

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
// Agent Handler Internal Error Tests (Review finding F-05, carry-forward F-01)
// Uses failingRegistry from server_helpers_test.go.
// =============================================================================

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
