package server

// agent_register_upsert_test.go — ISSUE-0125. POST /api/v1/agents/register no
// longer answers 409 CONFLICT on a re-registration: registry.Register is an
// upsert, so the row is replaced and the endpoint answers 201 for both an
// insert and an update ("registration accepted"). Split out of
// server_agent_test.go to keep that file under the 500-line review cap.

import (
	"net/http"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// TestRegisterAgentReRegisterUpdatesAddress pins the REST half of the ISSUE-0125
// precondition: a re-register against a POPULATED registry now updates the row
// instead of answering 409 CONFLICT, so an agent that came back on a new address
// can correct it. This is what makes the agent-side re-registration watcher
// useful against an orchestrator that never actually lost its registry.
func TestRegisterAgentReRegisterUpdatesAddress(t *testing.T) {
	srv, _ := testServer(t)
	body := []byte(`{"id": "code-writer", "address": "localhost:50051"}`)
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/agents/register", body)
	require.Equal(t, http.StatusCreated, rec.Code)

	moved := []byte(`{"id": "code-writer", "address": "10.0.0.7:50051"}`)
	rec = doRequest(srv.Handler(), http.MethodPost, "/api/v1/agents/register", moved)
	require.Equal(t, http.StatusCreated, rec.Code)
	assert.Contains(t, rec.Body.String(), "10.0.0.7:50051")

	rec = doRequest(srv.Handler(), http.MethodGet, "/api/v1/agents/code-writer", nil)
	require.Equal(t, http.StatusOK, rec.Code)
	assert.Contains(t, rec.Body.String(), "10.0.0.7:50051")
	assert.NotContains(t, rec.Body.String(), "localhost:50051")
}
