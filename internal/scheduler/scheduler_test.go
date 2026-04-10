package scheduler

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"

	"github.com/orchestr8/orchestr8/internal/executor"
	"github.com/orchestr8/orchestr8/internal/planner"
	"github.com/orchestr8/orchestr8/internal/state"
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

// --- Tests ---

func TestSingleStepEndToEnd(t *testing.T) {
	dir := t.TempDir()
	writeWorkflow(t, dir, "test-wf", singleStepYAML)

	store := state.NewInMemoryStore(zap.NewNop())
	exec := &mockExecutor{handler: func(_ context.Context, req executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		return &executor.ExecuteResult{
			TaskID: "t1",
			Output: "completed output",
		}, nil
	}}

	sched := newTestScheduler(t, store, exec, dir, WithPollInterval(50*time.Millisecond))
	createPendingRun(t, store, "run-1", "test-wf", nil)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go sched.Run(ctx)

	run := waitForRunStatus(t, store, "run-1", state.RunCompleted, 5*time.Second)
	assert.Equal(t, state.RunCompleted, run.Status)
	assert.False(t, run.StartedAt.IsZero(), "StartedAt should be set")
	assert.False(t, run.FinishedAt.IsZero(), "FinishedAt should be set")

	// Verify step state.
	step, ok := run.Steps["step1"]
	require.True(t, ok, "step1 should exist in steps map")
	assert.Equal(t, state.RunCompleted, step.Status)
	assert.Equal(t, "completed output", step.Output)
}

func TestMultiStageSequential(t *testing.T) {
	dir := t.TempDir()
	writeWorkflow(t, dir, "multi-stage", multiStageYAML)

	store := state.NewInMemoryStore(zap.NewNop())

	var order []string
	payloads := make(map[string]string) // agentID → resolved payload
	var orderMu sync.Mutex

	exec := &mockExecutor{handler: func(_ context.Context, req executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		orderMu.Lock()
		order = append(order, req.AgentID)
		payloads[req.AgentID] = req.Payload
		orderMu.Unlock()
		return &executor.ExecuteResult{
			TaskID: "t1",
			Output: fmt.Sprintf("output from %s", req.AgentID),
		}, nil
	}}

	sched := newTestScheduler(t, store, exec, dir, WithPollInterval(50*time.Millisecond))
	createPendingRun(t, store, "run-2", "multi-stage", nil)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go sched.Run(ctx)

	run := waitForRunStatus(t, store, "run-2", state.RunCompleted, 5*time.Second)
	assert.Equal(t, state.RunCompleted, run.Status)

	orderMu.Lock()
	defer orderMu.Unlock()
	require.Len(t, order, 3)
	assert.Equal(t, "agent-a", order[0])
	assert.Equal(t, "agent-b", order[1])
	assert.Equal(t, "agent-c", order[2])

	// Verify template resolution: agent-b receives agent-a's output via {{ steps.out1.output }}.
	assert.Equal(t, "output from agent-a", payloads["agent-b"], "agent-b should receive resolved template from agent-a output")
	assert.Equal(t, "output from agent-b", payloads["agent-c"], "agent-c should receive resolved template from agent-b output")
}

func TestParallelStage(t *testing.T) {
	dir := t.TempDir()
	writeWorkflow(t, dir, "parallel-wf", parallelStageYAML)

	store := state.NewInMemoryStore(zap.NewNop())

	var concurrentCount atomic.Int32
	var maxConcurrent atomic.Int32

	exec := &mockExecutor{handler: func(_ context.Context, req executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		cur := concurrentCount.Add(1)
		// Track max concurrent.
		for {
			old := maxConcurrent.Load()
			if cur <= old || maxConcurrent.CompareAndSwap(old, cur) {
				break
			}
		}
		time.Sleep(50 * time.Millisecond) // simulate work
		concurrentCount.Add(-1)
		return &executor.ExecuteResult{
			TaskID: "t1",
			Output: fmt.Sprintf("output from %s", req.AgentID),
		}, nil
	}}

	sched := newTestScheduler(t, store, exec, dir, WithPollInterval(50*time.Millisecond))
	createPendingRun(t, store, "run-3", "parallel-wf", nil)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go sched.Run(ctx)

	run := waitForRunStatus(t, store, "run-3", state.RunCompleted, 5*time.Second)
	assert.Equal(t, state.RunCompleted, run.Status)
	assert.GreaterOrEqual(t, maxConcurrent.Load(), int32(2), "steps a and b should execute in parallel")
}

func TestStepFailureFailsRun(t *testing.T) {
	dir := t.TempDir()
	writeWorkflow(t, dir, "test-wf", singleStepYAML)

	store := state.NewInMemoryStore(zap.NewNop())
	exec := &mockExecutor{handler: func(_ context.Context, req executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		return nil, fmt.Errorf("agent exploded")
	}}

	sched := newTestScheduler(t, store, exec, dir, WithPollInterval(50*time.Millisecond))
	createPendingRun(t, store, "run-4", "test-wf", nil)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go sched.Run(ctx)

	run := waitForRunStatus(t, store, "run-4", state.RunFailed, 5*time.Second)
	assert.Equal(t, state.RunFailed, run.Status)
	assert.Contains(t, run.Error, "agent exploded")
	assert.False(t, run.FinishedAt.IsZero(), "FinishedAt should be set on failure")

	// Verify step marked as failed.
	step, ok := run.Steps["step1"]
	require.True(t, ok)
	assert.Equal(t, state.RunFailed, step.Status)
	assert.Contains(t, step.Error, "agent exploded")
}

func TestPollLoopPicksUpPendingRun(t *testing.T) {
	dir := t.TempDir()
	writeWorkflow(t, dir, "test-wf", singleStepYAML)

	store := state.NewInMemoryStore(zap.NewNop())
	exec := &mockExecutor{handler: func(_ context.Context, _ executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		return &executor.ExecuteResult{TaskID: "t1", Output: "ok"}, nil
	}}

	sched := newTestScheduler(t, store, exec, dir, WithPollInterval(50*time.Millisecond))

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go sched.Run(ctx)

	// Create run after scheduler is running.
	time.Sleep(100 * time.Millisecond)
	createPendingRun(t, store, "delayed-run", "test-wf", nil)

	run := waitForRunStatus(t, store, "delayed-run", state.RunCompleted, 5*time.Second)
	assert.Equal(t, state.RunCompleted, run.Status)
}

func TestGracefulShutdown(t *testing.T) {
	dir := t.TempDir()
	writeWorkflow(t, dir, "test-wf", singleStepYAML)

	store := state.NewInMemoryStore(zap.NewNop())
	exec := &mockExecutor{handler: func(_ context.Context, _ executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		return &executor.ExecuteResult{TaskID: "t1", Output: "ok"}, nil
	}}

	sched := newTestScheduler(t, store, exec, dir, WithPollInterval(50*time.Millisecond))

	ctx, cancel := context.WithCancel(context.Background())
	errCh := make(chan error, 1)
	go func() {
		errCh <- sched.Run(ctx)
	}()

	cancel()
	err := <-errCh
	assert.ErrorIs(t, err, context.Canceled)
}

func TestGracefulShutdownBlockedGoroutines(t *testing.T) {
	dir := t.TempDir()
	writeWorkflow(t, dir, "test-wf", singleStepYAML)

	store := state.NewInMemoryStore(zap.NewNop())

	// Executor that blocks until context is cancelled.
	exec := &mockExecutor{handler: func(ctx context.Context, _ executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		<-ctx.Done()
		return nil, ctx.Err()
	}}

	// maxConcurrent=1 so second run blocks on semaphore acquisition.
	sched := newTestScheduler(t, store, exec, dir,
		WithPollInterval(50*time.Millisecond),
		WithMaxConcurrent(1),
	)

	createPendingRun(t, store, "block-1", "test-wf", nil)
	createPendingRun(t, store, "block-2", "test-wf", nil)

	ctx, cancel := context.WithCancel(context.Background())
	errCh := make(chan error, 1)
	go func() {
		errCh <- sched.Run(ctx)
	}()

	// Let both runs be picked up.
	time.Sleep(200 * time.Millisecond)
	cancel()

	err := <-errCh
	assert.ErrorIs(t, err, context.Canceled)
}

func TestConcurrentRunLimit(t *testing.T) {
	dir := t.TempDir()
	writeWorkflow(t, dir, "test-wf", singleStepYAML)

	store := state.NewInMemoryStore(zap.NewNop())

	var activeTasks atomic.Int32
	var maxActive atomic.Int32

	exec := &mockExecutor{handler: func(ctx context.Context, _ executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		cur := activeTasks.Add(1)
		for {
			old := maxActive.Load()
			if cur <= old || maxActive.CompareAndSwap(old, cur) {
				break
			}
		}
		time.Sleep(100 * time.Millisecond)
		activeTasks.Add(-1)
		return &executor.ExecuteResult{TaskID: "t1", Output: "ok"}, nil
	}}

	// Allow only 2 concurrent runs.
	sched := newTestScheduler(t, store, exec, dir,
		WithPollInterval(50*time.Millisecond),
		WithMaxConcurrent(2),
	)

	// Create 5 pending runs.
	for i := 0; i < 5; i++ {
		createPendingRun(t, store, fmt.Sprintf("conc-%d", i), "test-wf", nil)
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go sched.Run(ctx)

	// Wait for all runs to complete.
	for i := 0; i < 5; i++ {
		waitForRunStatus(t, store, fmt.Sprintf("conc-%d", i), state.RunCompleted, 10*time.Second)
	}

	// The max active should not exceed maxConcurrent + some slack for goroutine scheduling.
	// With maxConcurrent=2, we expect at most 2 runs executing simultaneously.
	assert.LessOrEqual(t, maxActive.Load(), int32(2), "should not exceed maxConcurrent")
}

func TestDuplicateRunPrevention(t *testing.T) {
	dir := t.TempDir()
	writeWorkflow(t, dir, "test-wf", singleStepYAML)

	store := state.NewInMemoryStore(zap.NewNop())

	// Executor that blocks until released.
	released := make(chan struct{})
	exec := &mockExecutor{handler: func(ctx context.Context, _ executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		select {
		case <-released:
		case <-ctx.Done():
			return nil, ctx.Err()
		}
		return &executor.ExecuteResult{TaskID: "t1", Output: "ok"}, nil
	}}

	sched := newTestScheduler(t, store, exec, dir, WithPollInterval(50*time.Millisecond))
	createPendingRun(t, store, "dup-1", "test-wf", nil)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	sem := make(chan struct{}, 10)

	// Call pollAndExecute twice — the second should not re-dispatch the same run.
	sched.pollAndExecute(ctx, sem)
	time.Sleep(50 * time.Millisecond) // let goroutine start
	sched.pollAndExecute(ctx, sem)
	time.Sleep(50 * time.Millisecond)

	// The executor should have been called at most once (the run was in-flight during the second poll).
	assert.Equal(t, int64(1), exec.calls.Load(), "run should only be dispatched once")

	close(released)
	waitForRunStatus(t, store, "dup-1", state.RunCompleted, 5*time.Second)
}

func TestEmptyPollCycle(t *testing.T) {
	dir := t.TempDir()

	store := state.NewInMemoryStore(zap.NewNop())
	exec := &mockExecutor{handler: func(_ context.Context, _ executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		t.Fatal("executor should not be called when no pending runs")
		return nil, nil
	}}

	sched := newTestScheduler(t, store, exec, dir, WithPollInterval(50*time.Millisecond))

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	sem := make(chan struct{}, 10)
	sched.pollAndExecute(ctx, sem)
	// No panic, no executor call — test passes if it reaches here.
}

func TestParseFailure(t *testing.T) {
	dir := t.TempDir()
	writeWorkflow(t, dir, "bad-wf", badYAML)

	store := state.NewInMemoryStore(zap.NewNop())
	exec := &mockExecutor{handler: func(_ context.Context, _ executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		t.Fatal("executor should not be called on parse failure")
		return nil, nil
	}}

	sched := newTestScheduler(t, store, exec, dir, WithPollInterval(50*time.Millisecond))
	createPendingRun(t, store, "bad-run", "bad-wf", nil)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go sched.Run(ctx)

	run := waitForRunStatus(t, store, "bad-run", state.RunFailed, 5*time.Second)
	assert.Contains(t, run.Error, "parse workflow")
}

func TestDAGCycleFailure(t *testing.T) {
	dir := t.TempDir()
	writeWorkflow(t, dir, "cycle-wf", cycleYAML)

	store := state.NewInMemoryStore(zap.NewNop())
	exec := &mockExecutor{handler: func(_ context.Context, _ executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		t.Fatal("executor should not be called on DAG failure")
		return nil, nil
	}}

	sched := newTestScheduler(t, store, exec, dir, WithPollInterval(50*time.Millisecond))
	createPendingRun(t, store, "cycle-run", "cycle-wf", nil)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go sched.Run(ctx)

	run := waitForRunStatus(t, store, "cycle-run", state.RunFailed, 5*time.Second)
	assert.Contains(t, run.Error, "validate DAG")
}

func TestResolveWorkflowPath(t *testing.T) {
	got, err := resolveWorkflowPath("/workflows", "my-feature")
	require.NoError(t, err)
	// Use filepath.Join to get OS-appropriate separator.
	expected := filepath.Join("/workflows", "my-feature.yaml")
	assert.Equal(t, expected, got)
}

func TestResolveWorkflowPath_InvalidID(t *testing.T) {
	_, err := resolveWorkflowPath("/workflows", "../etc/passwd")
	assert.Error(t, err)
	assert.Contains(t, err.Error(), "invalid workflow ID format")
}

func TestParallelStepFailures(t *testing.T) {
	// When 2 parallel steps both fail, all errors are joined at the run level
	// (RFC 0003 §executeStage). Individual steps also record their own errors.
	dir := t.TempDir()
	writeWorkflow(t, dir, "parallel-wf", parallelStageYAML)

	store := state.NewInMemoryStore(zap.NewNop())
	exec := &mockExecutor{handler: func(_ context.Context, req executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		return nil, fmt.Errorf("%s failed", req.AgentID)
	}}

	sched := newTestScheduler(t, store, exec, dir, WithPollInterval(50*time.Millisecond))
	createPendingRun(t, store, "par-fail", "parallel-wf", nil)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go sched.Run(ctx)

	run := waitForRunStatus(t, store, "par-fail", state.RunFailed, 5*time.Second)
	assert.Equal(t, state.RunFailed, run.Status)
	assert.NotEmpty(t, run.Error, "run-level error should be set")

	// Both individual steps should be marked as failed with their own errors.
	stepA, okA := run.Steps["a"]
	stepB, okB := run.Steps["b"]
	require.True(t, okA, "step a should exist")
	require.True(t, okB, "step b should exist")
	assert.Equal(t, state.RunFailed, stepA.Status)
	assert.Equal(t, state.RunFailed, stepB.Status)
	assert.Contains(t, stepA.Error, "agent-x failed")
	assert.Contains(t, stepB.Error, "agent-y failed")
}

func TestExecuteStepPassesWorkflowID(t *testing.T) {
	// Verify the executor receives the workflow ID (e.g., "test-wf"), not the run UUID.
	dir := t.TempDir()
	writeWorkflow(t, dir, "test-wf", singleStepYAML)

	store := state.NewInMemoryStore(zap.NewNop())
	var receivedWorkflowID string
	exec := &mockExecutor{handler: func(_ context.Context, req executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		receivedWorkflowID = req.WorkflowID
		return &executor.ExecuteResult{TaskID: "t1", Output: "ok"}, nil
	}}

	sched := newTestScheduler(t, store, exec, dir, WithPollInterval(50*time.Millisecond))
	createPendingRun(t, store, "wfid-run", "test-wf", nil)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go sched.Run(ctx)

	waitForRunStatus(t, store, "wfid-run", state.RunCompleted, 5*time.Second)
	assert.Equal(t, "test-wf", receivedWorkflowID, "executor should receive workflow ID, not run UUID")
}
