package server

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/mkhomutov/persatrix/internal/registry"
)

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

// RFC 0048 amendment §A — role round-trips through register → response so the
// console picker can show the persona's role, not just its name.
func TestRegisterAgentWithRole(t *testing.T) {
	srv, _ := testServer(t)
	body := []byte(`{"id": "code-writer", "name": "Code Writer", "role": "Senior Engineer", "address": "localhost:50051"}`)
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/agents/register", body)
	require.Equal(t, http.StatusCreated, rec.Code)

	var resp agentResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	assert.Equal(t, "Senior Engineer", resp.Role)
}

// An omitted role is valid (not every agent declares one) and serializes as an
// empty string — the client falls back to showing no role, matching name→id.
func TestRegisterAgentRoleDefaultsEmpty(t *testing.T) {
	srv, _ := testServer(t)
	body := []byte(`{"id": "no-role", "address": "localhost:50051"}`)
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/agents/register", body)
	require.Equal(t, http.StatusCreated, rec.Code)

	var resp agentResponse
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &resp))
	assert.Empty(t, resp.Role)
	assert.Contains(t, rec.Body.String(), `"role":""`)
}

// Role is display-only and capped like name to prevent registry pollution.
func TestRegisterAgentRoleTooLong(t *testing.T) {
	srv, _ := testServer(t)
	body, _ := json.Marshal(registerAgentRequest{
		ID:      "long-role",
		Role:    strings.Repeat("x", 101),
		Address: "localhost:50051",
	})
	rec := doRequest(srv.Handler(), http.MethodPost, "/api/v1/agents/register", body)
	assert.Equal(t, http.StatusBadRequest, rec.Code)
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

// --- Agent Registration Edge Cases (PR #16 carry-forward F-01, F-03) ---

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
