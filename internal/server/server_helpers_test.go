package server

import (
	"bytes"
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

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

// =============================================================================
// failingStore — forces specific Store methods to return non-sentinel errors,
// enabling 500 error-path coverage for workflow handlers.
// =============================================================================

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

// =============================================================================
// failingRegistry — forces specific Registry methods to return non-sentinel
// errors, enabling 500 error-path coverage for agent handlers.
// =============================================================================

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
