package server

import (
	"encoding/json"
	"net/http"
	"strings"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// RFC 0048 amendment §A DTO — the agent `type` ("task"/"persona") round-trips through
// register → response so the web console can tell a conversational persona from a
// workflow task agent and disable chat for the latter. These mirror the §A `role`
// handler tests; kept in a separate file because server_agent_test.go sits at the
// 500-line cap.

func TestRegisterAgentWithType(t *testing.T) {
	srv, _ := testServer(t)
	body := []byte(`{"id": "planner", "name": "Planner", "type": "task", "address": "localhost:50051"}`)
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/agents/register", body)
	require.Equal(t, http.StatusCreated, rec.Code)

	var resp agentResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	assert.Equal(t, "task", resp.Type)
}

// An omitted type is valid (an agent predating the field) and serializes as an
// empty string — the console treats "" as chattable, so nothing is disabled.
func TestRegisterAgentTypeDefaultsEmpty(t *testing.T) {
	srv, _ := testServer(t)
	body := []byte(`{"id": "no-type", "address": "localhost:50051"}`)
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/agents/register", body)
	require.Equal(t, http.StatusCreated, rec.Code)

	var resp agentResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	assert.Empty(t, resp.Type)
	assert.Contains(t, rec.Body.String(), `"type":""`)
}

// Type is a short kind token, capped tightly to prevent registry pollution.
func TestRegisterAgentTypeTooLong(t *testing.T) {
	srv, _ := testServer(t)
	body, _ := json.Marshal(registerAgentRequest{
		ID:      "long-type",
		Type:    strings.Repeat("x", 33),
		Address: "localhost:50051",
	})
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/agents/register", body)
	assert.Equal(t, http.StatusBadRequest, rec.Code)
}

// List surfaces the stored type so the picker can filter — a registered task
// agent comes back tagged "task".
func TestListAgentsCarriesType(t *testing.T) {
	srv, _ := testServer(t)
	body := []byte(`{"id": "planner", "type": "task", "address": "localhost:50051"}`)
	require.Equal(t, http.StatusCreated,
		doRequest(srv.Handler(), http.MethodPost, "/api/v1/agents/register", body).Code)

	rec := doRequest(srv.Handler(), http.MethodGet, "/api/v1/agents", nil)
	require.Equal(t, http.StatusOK, rec.Code)
	var list []agentResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &list))
	require.Len(t, list, 1)
	assert.Equal(t, "task", list[0].Type)
}
