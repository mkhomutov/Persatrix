package scheduler

import (
	"context"
	"os"
	"path/filepath"
	"sync/atomic"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/executor"
	"github.com/mkhomutov/persatrix/internal/planner"
	"github.com/mkhomutov/persatrix/internal/state"
)

// --- Mock executor ---

type mockExecutor struct {
	handler func(ctx context.Context, req executor.ExecuteRequest) (*executor.ExecuteResult, error)
	calls   atomic.Int64
}

func (m *mockExecutor) ExecuteTask(ctx context.Context, req executor.ExecuteRequest) (*executor.ExecuteResult, error) {
	m.calls.Add(1)
	return m.handler(ctx, req)
}

func (m *mockExecutor) Close() error { return nil }

// --- Test helpers ---

// writeWorkflow writes a minimal workflow YAML into dir.
func writeWorkflow(t *testing.T, dir, workflowID, content string) {
	t.Helper()
	err := os.WriteFile(filepath.Join(dir, workflowID+".yaml"), []byte(content), 0o644)
	require.NoError(t, err)
}

const singleStepYAML = `schema_version: "0.1"
workflow:
  id: "test-wf"
  name: "Test Workflow"
  trigger: "manual"
  steps:
    - id: "step1"
      agent: "test-agent"
      input: "do something"
      output_key: "result"
`

const multiStageYAML = `schema_version: "0.1"
workflow:
  id: "multi-stage"
  name: "Multi Stage"
  trigger: "manual"
  steps:
    - id: "s1"
      agent: "agent-a"
      input: "first task"
      output_key: "out1"
    - id: "s2"
      agent: "agent-b"
      input: "{{ steps.out1.output }}"
      output_key: "out2"
      depends_on: ["s1"]
    - id: "s3"
      agent: "agent-c"
      input: "{{ steps.out2.output }}"
      output_key: "out3"
      depends_on: ["s2"]
`

const parallelStageYAML = `schema_version: "0.1"
workflow:
  id: "parallel-wf"
  name: "Parallel Workflow"
  trigger: "manual"
  steps:
    - id: "a"
      agent: "agent-x"
      input: "task a"
      output_key: "a_out"
    - id: "b"
      agent: "agent-y"
      input: "task b"
      output_key: "b_out"
`

const badYAML = `this is not valid yaml: [[[`

const cycleYAML = `schema_version: "0.1"
workflow:
  id: "cycle-wf"
  name: "Cycle"
  trigger: "manual"
  steps:
    - id: "s1"
      agent: "agent-a"
      input: "first"
      depends_on: ["s2"]
    - id: "s2"
      agent: "agent-b"
      input: "second"
      depends_on: ["s1"]
`

func newTestScheduler(t *testing.T, store state.Store, exec executor.Executor, workflowsDir string, opts ...Option) *WorkflowScheduler {
	t.Helper()
	logger := zap.NewNop()
	plan := planner.NewYAMLPlanner(logger)
	return NewWorkflowScheduler(store, nil, plan, exec, logger, workflowsDir, opts...)
}

func createPendingRun(t *testing.T, store state.Store, runID, workflowID string, inputs map[string]string) {
	t.Helper()
	run := &state.WorkflowRun{
		ID:         runID,
		WorkflowID: workflowID,
		Status:     state.RunPending,
		Inputs:     inputs,
	}
	require.NoError(t, store.CreateRun(context.Background(), run))
}

// waitForRunStatus polls the store until the run reaches the expected status or timeout.
func waitForRunStatus(t *testing.T, store state.Store, runID string, expected state.RunStatus, timeout time.Duration) *state.WorkflowRun {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		run, err := store.GetRun(context.Background(), runID)
		if err == nil && run.Status == expected {
			return run
		}
		time.Sleep(10 * time.Millisecond)
	}
	run, err := store.GetRun(context.Background(), runID)
	require.NoError(t, err)
	require.Equal(t, expected, run.Status, "run %s did not reach status %s within %v (current: %s)", runID, expected, timeout, run.Status)
	return run
}
