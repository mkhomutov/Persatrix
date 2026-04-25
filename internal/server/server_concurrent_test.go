package server

import (
	"encoding/json"
	"fmt"
	"net/http"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

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
