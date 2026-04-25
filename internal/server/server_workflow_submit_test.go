package server

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

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

// --- Inputs with nil ---

func TestSubmitWorkflowRunNoInputs(t *testing.T) {
	srv, dir := testServer(t)
	writeWorkflowFixture(t, dir, "test-wf")

	body := []byte(`{"workflow_id": "test-wf"}`)
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/workflows/run", body)
	assert.Equal(t, http.StatusCreated, rec.Code)
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
