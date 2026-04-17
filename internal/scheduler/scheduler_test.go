package scheduler

import (
	"context"
	"errors"
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
	"go.uber.org/zap/zaptest/observer"

	"github.com/mkhomutov/persatrix/internal/cost"
	"github.com/mkhomutov/persatrix/internal/executor"
	"github.com/mkhomutov/persatrix/internal/planner"
	"github.com/mkhomutov/persatrix/internal/registry"
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
	go func() { _ = sched.Run(ctx) }()

	run := waitForRunStatus(t, store, "run-1", state.RunCompleted, 5*time.Second)
	assert.Equal(t, state.RunCompleted, run.Status)
	assert.False(t, run.StartedAt.IsZero(), "StartedAt should be set")
	assert.False(t, run.FinishedAt.IsZero(), "FinishedAt should be set")

	// Verify step state.
	step, ok := run.Steps["step1"]
	require.True(t, ok, "step1 should exist in steps map")
	assert.Equal(t, state.RunCompleted, step.Status)
	assert.Equal(t, "completed output", step.Output)
	assert.False(t, step.StartedAt.IsZero(), "step StartedAt should be set")
	assert.False(t, step.FinishedAt.IsZero(), "step FinishedAt should be set")
	assert.True(t, step.FinishedAt.After(step.StartedAt) || step.FinishedAt.Equal(step.StartedAt),
		"step FinishedAt should be >= StartedAt")
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
	go func() { _ = sched.Run(ctx) }()

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
	go func() { _ = sched.Run(ctx) }()

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
	go func() { _ = sched.Run(ctx) }()

	run := waitForRunStatus(t, store, "run-4", state.RunFailed, 5*time.Second)
	assert.Equal(t, state.RunFailed, run.Status)
	assert.Contains(t, run.Error, "agent exploded")
	assert.False(t, run.FinishedAt.IsZero(), "FinishedAt should be set on failure")

	// Verify step marked as failed.
	step, ok := run.Steps["step1"]
	require.True(t, ok)
	assert.Equal(t, state.RunFailed, step.Status)
	assert.Contains(t, step.Error, "agent exploded")
	assert.False(t, step.StartedAt.IsZero(), "step StartedAt should be preserved on failure")
	assert.False(t, step.FinishedAt.IsZero(), "step FinishedAt should be set on failure")
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
	go func() { _ = sched.Run(ctx) }()

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
	go func() { _ = sched.Run(ctx) }()

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
	go func() { _ = sched.Run(ctx) }()

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
	go func() { _ = sched.Run(ctx) }()

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
	go func() { _ = sched.Run(ctx) }()

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
	go func() { _ = sched.Run(ctx) }()

	waitForRunStatus(t, store, "wfid-run", state.RunCompleted, 5*time.Second)
	assert.Equal(t, "test-wf", receivedWorkflowID, "executor should receive workflow ID, not run UUID")
}

func TestInputsTemplateResolution(t *testing.T) {
	// G-02: Verify that {{ variable }} templates are resolved from run.Inputs (the vars map).
	const inputsYAML = `schema_version: "0.1"
workflow:
  id: "inputs-wf"
  name: "Inputs Workflow"
  trigger: "manual"
  steps:
    - id: "s1"
      agent: "test-agent"
      input: "build: {{ user_request }}"
      output_key: "result"
`
	dir := t.TempDir()
	writeWorkflow(t, dir, "inputs-wf", inputsYAML)

	store := state.NewInMemoryStore(zap.NewNop())
	var receivedPayload string
	exec := &mockExecutor{handler: func(_ context.Context, req executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		receivedPayload = req.Payload
		return &executor.ExecuteResult{TaskID: "t1", Output: "ok"}, nil
	}}

	sched := newTestScheduler(t, store, exec, dir, WithPollInterval(50*time.Millisecond))
	createPendingRun(t, store, "inputs-run", "inputs-wf", map[string]string{
		"user_request": "a REST API",
	})

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = sched.Run(ctx) }()

	waitForRunStatus(t, store, "inputs-run", state.RunCompleted, 5*time.Second)
	assert.Equal(t, "build: a REST API", receivedPayload,
		"{{ user_request }} should be resolved from run.Inputs")
}

func TestFailRunWithCancelledContext(t *testing.T) {
	// G-03: Verify that failRun persists state even after the parent context is cancelled,
	// thanks to context.WithoutCancel. A long-running executor is cancelled mid-flight,
	// and the failure state should still be recorded.
	dir := t.TempDir()
	writeWorkflow(t, dir, "test-wf", singleStepYAML)

	store := state.NewInMemoryStore(zap.NewNop())

	stepStarted := make(chan struct{})
	exec := &mockExecutor{handler: func(ctx context.Context, _ executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		close(stepStarted)
		<-ctx.Done()
		return nil, ctx.Err()
	}}

	sched := newTestScheduler(t, store, exec, dir, WithPollInterval(50*time.Millisecond))
	createPendingRun(t, store, "cancel-run", "test-wf", nil)

	ctx, cancel := context.WithCancel(context.Background())
	go func() { _ = sched.Run(ctx) }()

	// Wait until the executor is actually running, then cancel.
	<-stepStarted
	cancel()

	// The run should reach Failed — failRun uses context.WithoutCancel to persist state.
	run := waitForRunStatus(t, store, "cancel-run", state.RunFailed, 5*time.Second)
	assert.Equal(t, state.RunFailed, run.Status)
	assert.NotEmpty(t, run.Error, "run error should be recorded despite cancelled context")
	assert.False(t, run.FinishedAt.IsZero(), "FinishedAt should be set despite cancelled context")
}

func TestSuccessPathContextCancellation(t *testing.T) {
	// G-01 / F-01 regression: Verify that the success-path state persistence
	// uses context.WithoutCancel, so a well-timed shutdown after all stages
	// complete does NOT leave the run stuck in Running.
	dir := t.TempDir()
	writeWorkflow(t, dir, "test-wf", singleStepYAML)

	store := state.NewInMemoryStore(zap.NewNop())

	stepDone := make(chan struct{})
	exec := &mockExecutor{handler: func(_ context.Context, _ executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		// Signal that the step completed, then give the test goroutine a
		// moment to cancel the context before executeRun reaches the
		// success-path UpdateRunStatus call.
		close(stepDone)
		time.Sleep(50 * time.Millisecond)
		return &executor.ExecuteResult{TaskID: "t1", Output: "ok"}, nil
	}}

	sched := newTestScheduler(t, store, exec, dir, WithPollInterval(50*time.Millisecond))
	createPendingRun(t, store, "success-cancel", "test-wf", nil)

	ctx, cancel := context.WithCancel(context.Background())
	go func() { _ = sched.Run(ctx) }()

	// Cancel the context right after the executor finishes but before
	// the scheduler persists the Completed status.
	<-stepDone
	cancel()

	// The run must reach Completed (not remain stuck at Running).
	run := waitForRunStatus(t, store, "success-cancel", state.RunCompleted, 5*time.Second)
	assert.Equal(t, state.RunCompleted, run.Status)
	assert.False(t, run.FinishedAt.IsZero(), "FinishedAt should be set despite cancelled context")
}

// --- PR 3b: Step Execution & Template Resolution Tests ---

// mockPlanner wraps a real YAMLPlanner but allows Plan() to be overridden.
type mockPlanner struct {
	real    planner.Planner
	planErr error // if non-nil, Plan() returns this error
}

func (m *mockPlanner) Parse(ctx context.Context, yamlPath string) (*planner.Workflow, error) {
	return m.real.Parse(ctx, yamlPath)
}

func (m *mockPlanner) ValidateDAG(ctx context.Context, wf *planner.Workflow) error {
	return m.real.ValidateDAG(ctx, wf)
}

func (m *mockPlanner) Plan(ctx context.Context, wf *planner.Workflow) (*planner.ExecutionPlan, error) {
	if m.planErr != nil {
		return nil, m.planErr
	}
	return m.real.Plan(ctx, wf)
}

// mockStore wraps InMemoryStore but allows ListRuns to be overridden.
type mockStore struct {
	state.Store
	listRunsErr error // if non-nil, ListRuns() returns this error
}

func (m *mockStore) ListRuns(ctx context.Context) ([]*state.WorkflowRun, error) {
	if m.listRunsErr != nil {
		return nil, m.listRunsErr
	}
	return m.Store.ListRuns(ctx)
}

func TestResolutionFailureMissingVariable(t *testing.T) {
	// Missing variable in {{ variable }} template → RunFailed with resolution error.
	const missingVarYAML = `schema_version: "0.1"
workflow:
  id: "missing-var"
  name: "Missing Var"
  trigger: "manual"
  steps:
    - id: "s1"
      agent: "test-agent"
      input: "build: {{ missing_key }}"
      output_key: "result"
`
	dir := t.TempDir()
	writeWorkflow(t, dir, "missing-var", missingVarYAML)

	store := state.NewInMemoryStore(zap.NewNop())
	exec := &mockExecutor{handler: func(_ context.Context, _ executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		t.Fatal("executor should not be called when template resolution fails")
		return nil, nil
	}}

	sched := newTestScheduler(t, store, exec, dir, WithPollInterval(50*time.Millisecond))
	createPendingRun(t, store, "missing-run", "missing-var", nil) // no inputs → missing_key unresolvable

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = sched.Run(ctx) }()

	run := waitForRunStatus(t, store, "missing-run", state.RunFailed, 5*time.Second)
	assert.Equal(t, state.RunFailed, run.Status)
	assert.Contains(t, run.Error, "missing_key", "error should mention the missing variable")

	// Step should be marked as failed with the resolution error.
	step, ok := run.Steps["s1"]
	require.True(t, ok, "step s1 should exist")
	assert.Equal(t, state.RunFailed, step.Status)
	assert.Contains(t, step.Error, "missing_key")
}

func TestResolutionFailureMissingStepOutput(t *testing.T) {
	// Missing step output in {{ steps.<id>.output }} template → RunFailed.
	const missingStepYAML = `schema_version: "0.1"
workflow:
  id: "missing-step"
  name: "Missing Step Output"
  trigger: "manual"
  steps:
    - id: "s1"
      agent: "test-agent"
      input: "use: {{ steps.nonexistent.output }}"
      output_key: "result"
`
	dir := t.TempDir()
	writeWorkflow(t, dir, "missing-step", missingStepYAML)

	store := state.NewInMemoryStore(zap.NewNop())
	exec := &mockExecutor{handler: func(_ context.Context, _ executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		t.Fatal("executor should not be called when step output is missing")
		return nil, nil
	}}

	sched := newTestScheduler(t, store, exec, dir, WithPollInterval(50*time.Millisecond))
	createPendingRun(t, store, "missing-step-run", "missing-step", nil)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = sched.Run(ctx) }()

	run := waitForRunStatus(t, store, "missing-step-run", state.RunFailed, 5*time.Second)
	assert.Equal(t, state.RunFailed, run.Status)
	assert.Contains(t, run.Error, "nonexistent", "error should mention the missing step ID")

	// N-35: Step-level assertions — verify step is marked failed with resolution error.
	step, ok := run.Steps["s1"]
	require.True(t, ok, "step s1 should exist")
	assert.Equal(t, state.RunFailed, step.Status)
	assert.Contains(t, step.Error, "nonexistent", "step error should mention the missing step ID")
}

func TestStepStateTransitionsSuccess(t *testing.T) {
	// Verify a successful step transitions through Running → Completed with timestamps.
	dir := t.TempDir()
	writeWorkflow(t, dir, "test-wf", singleStepYAML)

	store := state.NewInMemoryStore(zap.NewNop())

	// Record intermediate step states.
	var midRunStepStatus state.RunStatus
	var midRunStepHasStartedAt bool
	stepExecuting := make(chan struct{})
	stepContinue := make(chan struct{})

	exec := &mockExecutor{handler: func(ctx context.Context, _ executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		close(stepExecuting)
		<-stepContinue
		return &executor.ExecuteResult{TaskID: "t1", Output: "done"}, nil
	}}

	sched := newTestScheduler(t, store, exec, dir, WithPollInterval(50*time.Millisecond))
	createPendingRun(t, store, "trans-run", "test-wf", nil)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = sched.Run(ctx) }()

	// Wait for executor to be called — step should be in Running state.
	<-stepExecuting
	run, err := store.GetRun(context.Background(), "trans-run")
	require.NoError(t, err)
	if step, ok := run.Steps["step1"]; ok {
		midRunStepStatus = step.Status
		midRunStepHasStartedAt = !step.StartedAt.IsZero()
	}
	close(stepContinue)

	// Wait for completion.
	run = waitForRunStatus(t, store, "trans-run", state.RunCompleted, 5*time.Second)

	// Mid-execution: step was Running with StartedAt set.
	assert.Equal(t, state.RunRunning, midRunStepStatus, "step should be Running during execution")
	assert.True(t, midRunStepHasStartedAt, "step StartedAt should be set during execution")

	// Post-completion: step is Completed with both timestamps.
	step, ok := run.Steps["step1"]
	require.True(t, ok)
	assert.Equal(t, state.RunCompleted, step.Status)
	assert.False(t, step.StartedAt.IsZero(), "step StartedAt should be set")
	assert.False(t, step.FinishedAt.IsZero(), "step FinishedAt should be set")
	assert.True(t, step.FinishedAt.After(step.StartedAt) || step.FinishedAt.Equal(step.StartedAt))
}

func TestStepStateTransitionsFailure(t *testing.T) {
	// Verify a failing step transitions through Running → Failed with error and timestamps.
	dir := t.TempDir()
	writeWorkflow(t, dir, "test-wf", singleStepYAML)

	store := state.NewInMemoryStore(zap.NewNop())

	var midRunStepStatus state.RunStatus
	var midRunStepHasStartedAt bool
	stepExecuting := make(chan struct{})
	stepContinue := make(chan struct{})

	exec := &mockExecutor{handler: func(ctx context.Context, _ executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		close(stepExecuting)
		<-stepContinue
		return nil, fmt.Errorf("step failed intentionally")
	}}

	sched := newTestScheduler(t, store, exec, dir, WithPollInterval(50*time.Millisecond))
	createPendingRun(t, store, "fail-trans", "test-wf", nil)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = sched.Run(ctx) }()

	// Wait for executor to be called — step should be in Running state.
	<-stepExecuting
	run, err := store.GetRun(context.Background(), "fail-trans")
	require.NoError(t, err)
	if step, ok := run.Steps["step1"]; ok {
		midRunStepStatus = step.Status
		midRunStepHasStartedAt = !step.StartedAt.IsZero()
	}
	close(stepContinue)

	// Wait for run failure.
	run = waitForRunStatus(t, store, "fail-trans", state.RunFailed, 5*time.Second)

	// Mid-execution: step was Running with StartedAt set (symmetry with success variant).
	assert.Equal(t, state.RunRunning, midRunStepStatus, "step should be Running during execution")
	assert.True(t, midRunStepHasStartedAt, "step StartedAt should be set during execution")

	// Post-failure: step is Failed with error and both timestamps.
	step, ok := run.Steps["step1"]
	require.True(t, ok)
	assert.Equal(t, state.RunFailed, step.Status)
	assert.Contains(t, step.Error, "step failed intentionally")
	assert.False(t, step.StartedAt.IsZero(), "step StartedAt should be preserved on failure")
	assert.False(t, step.FinishedAt.IsZero(), "step FinishedAt should be set on failure")
}

func TestMultiStageTemplateChaining(t *testing.T) {
	// Verify output from stage N is available as input in stage N+1 via {{ steps.<key>.output }}.
	const chainingYAML = `schema_version: "0.1"
workflow:
  id: "chain-wf"
  name: "Chain"
  trigger: "manual"
  steps:
    - id: "generate"
      agent: "gen-agent"
      input: "generate data for {{ user_request }}"
      output_key: "gen_out"
    - id: "transform"
      agent: "xform-agent"
      input: "transform: {{ steps.gen_out.output }}"
      output_key: "xform_out"
      depends_on: ["generate"]
    - id: "validate"
      agent: "val-agent"
      input: "validate: {{ steps.xform_out.output }}"
      output_key: "val_out"
      depends_on: ["transform"]
`
	dir := t.TempDir()
	writeWorkflow(t, dir, "chain-wf", chainingYAML)

	store := state.NewInMemoryStore(zap.NewNop())
	payloads := make(map[string]string)
	var mu sync.Mutex

	exec := &mockExecutor{handler: func(_ context.Context, req executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		mu.Lock()
		payloads[req.AgentID] = req.Payload
		mu.Unlock()
		return &executor.ExecuteResult{
			TaskID: "t1",
			Output: fmt.Sprintf("result-from-%s", req.AgentID),
		}, nil
	}}

	sched := newTestScheduler(t, store, exec, dir, WithPollInterval(50*time.Millisecond))
	createPendingRun(t, store, "chain-run", "chain-wf", map[string]string{
		"user_request": "API endpoints",
	})

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = sched.Run(ctx) }()

	run := waitForRunStatus(t, store, "chain-run", state.RunCompleted, 5*time.Second)
	assert.Equal(t, state.RunCompleted, run.Status)

	mu.Lock()
	defer mu.Unlock()
	assert.Equal(t, "generate data for API endpoints", payloads["gen-agent"],
		"first stage should resolve {{ user_request }} from inputs")
	assert.Equal(t, "transform: result-from-gen-agent", payloads["xform-agent"],
		"second stage should resolve {{ steps.gen_out.output }} from first stage output")
	assert.Equal(t, "validate: result-from-xform-agent", payloads["val-agent"],
		"third stage should resolve {{ steps.xform_out.output }} from second stage output")
}

func TestTOCTOUBranch(t *testing.T) {
	// N-24: Run changed to RunCancelled between poll and executeRun re-read → no execution.
	dir := t.TempDir()
	writeWorkflow(t, dir, "test-wf", singleStepYAML)

	store := state.NewInMemoryStore(zap.NewNop())
	exec := &mockExecutor{handler: func(_ context.Context, _ executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		t.Fatal("executor should not be called for a cancelled run")
		return nil, nil
	}}

	sched := newTestScheduler(t, store, exec, dir, WithPollInterval(50*time.Millisecond))
	createPendingRun(t, store, "toctou-run", "test-wf", nil)

	// Cancel the run between poll and execution.
	require.NoError(t, store.UpdateRunStatus(context.Background(), "toctou-run", state.RunCancelled))

	// Directly call executeRun — it should see RunCancelled and skip.
	sched.executeRun(context.Background(), "toctou-run")

	// Run should still be cancelled — not failed or completed.
	run, err := store.GetRun(context.Background(), "toctou-run")
	require.NoError(t, err)
	assert.Equal(t, state.RunCancelled, run.Status, "run should remain cancelled (TOCTOU guard)")
	assert.Equal(t, int64(0), exec.calls.Load(), "executor should not have been called")
}

func TestPlanErrorPath(t *testing.T) {
	// N-25: Planner.Plan() returns error → RunFailed with appropriate error message.
	dir := t.TempDir()
	writeWorkflow(t, dir, "test-wf", singleStepYAML)

	store := state.NewInMemoryStore(zap.NewNop())
	exec := &mockExecutor{handler: func(_ context.Context, _ executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		t.Fatal("executor should not be called when Plan() fails")
		return nil, nil
	}}

	logger := zap.NewNop()
	mp := &mockPlanner{
		real:    planner.NewYAMLPlanner(logger),
		planErr: fmt.Errorf("unsupported step type: custom-action"),
	}

	sched := NewWorkflowScheduler(store, nil, mp, exec, logger, dir, WithPollInterval(50*time.Millisecond))
	createPendingRun(t, store, "plan-err-run", "test-wf", nil)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = sched.Run(ctx) }()

	run := waitForRunStatus(t, store, "plan-err-run", state.RunFailed, 5*time.Second)
	assert.Equal(t, state.RunFailed, run.Status)
	assert.Contains(t, run.Error, "plan workflow")
	assert.Contains(t, run.Error, "unsupported step type")
}

func TestListRunsErrorPath(t *testing.T) {
	// N-26: Store.ListRuns() returns error → logged, no runs dispatched.
	// N-36: Use zap/observer to assert the error is actually logged.
	dir := t.TempDir()

	realStore := state.NewInMemoryStore(zap.NewNop())
	ms := &mockStore{
		Store:       realStore,
		listRunsErr: fmt.Errorf("database connection lost"),
	}

	exec := &mockExecutor{handler: func(_ context.Context, _ executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		t.Fatal("executor should not be called when ListRuns fails")
		return nil, nil
	}}

	// N-36: Use DebugLevel observer so the entry.Level assertion below
	// actually validates that production code logs at Error (not Warn/Info).
	// With ErrorLevel filter the level check would be tautologically true.
	core, logs := observer.New(zap.DebugLevel)
	observedLogger := zap.New(core)

	sched := NewWorkflowScheduler(ms, nil, planner.NewYAMLPlanner(zap.NewNop()), exec, observedLogger, dir, WithPollInterval(50*time.Millisecond))

	ctx := context.Background()
	sem := make(chan struct{}, 10)

	// pollAndExecute should handle the error gracefully — no panic, no dispatch.
	sched.pollAndExecute(ctx, sem)

	assert.Equal(t, int64(0), exec.calls.Load(), "executor should not be called on ListRuns error")

	// Verify the error was actually logged (prevents silent deletion of the log line).
	require.Equal(t, 1, logs.Len(), "expected exactly one error log entry")
	entry := logs.All()[0]
	assert.Equal(t, "failed to list runs", entry.Message)
	assert.Equal(t, zap.ErrorLevel, entry.Level)

	// Verify the structured zap.Error(err) field propagates the error value.
	// This detects regressions where the zap.Error(err) field is accidentally
	// removed from the log call but the message string remains.
	errVal, ok := entry.ContextMap()["error"]
	require.True(t, ok, "log entry should contain 'error' field from zap.Error()")
	assert.Contains(t, fmt.Sprintf("%v", errVal), "database connection lost")
}

// --- PR 1b: resolveStepLimits cascade tests ---

func TestResolveStepLimits_SystemDefaults(t *testing.T) {
	// Both step and agent have zero limits → system defaults used.
	store := state.NewInMemoryStore(zap.NewNop())
	reg := newTestRegistry(t)
	registerAgentWithLimits(t, reg, "test-agent", 0, 0, 0)

	sched := newTestSchedulerWithRegistry(t, store, &mockExecutor{}, reg, t.TempDir())
	step := planner.Step{AgentID: "test-agent"}

	limits := sched.resolveStepLimits(context.Background(), step)

	assert.Equal(t, 5, limits.MaxLLMCalls, "should use DefaultMaxLLMCalls")
	assert.Equal(t, 8192, limits.MaxTokens, "should use DefaultMaxTokens")
	assert.Equal(t, 60, limits.TimeoutSeconds, "should use DefaultTimeoutSeconds")
}

func TestResolveStepLimits_AgentOverridesDefaults(t *testing.T) {
	store := state.NewInMemoryStore(zap.NewNop())
	reg := newTestRegistry(t)
	registerAgentWithLimits(t, reg, "custom-agent", 8, 16384, 120)

	sched := newTestSchedulerWithRegistry(t, store, &mockExecutor{}, reg, t.TempDir())
	step := planner.Step{AgentID: "custom-agent"}

	limits := sched.resolveStepLimits(context.Background(), step)

	assert.Equal(t, 8, limits.MaxLLMCalls)
	assert.Equal(t, 16384, limits.MaxTokens)
	assert.Equal(t, 120, limits.TimeoutSeconds)
}

func TestResolveStepLimits_StepOverridesAgent(t *testing.T) {
	store := state.NewInMemoryStore(zap.NewNop())
	reg := newTestRegistry(t)
	registerAgentWithLimits(t, reg, "custom-agent", 8, 16384, 120)

	sched := newTestSchedulerWithRegistry(t, store, &mockExecutor{}, reg, t.TempDir())
	step := planner.Step{
		AgentID:        "custom-agent",
		MaxLLMCalls:    3,
		MaxTokens:      2048,
		TimeoutSeconds: 30,
	}

	limits := sched.resolveStepLimits(context.Background(), step)

	assert.Equal(t, 3, limits.MaxLLMCalls)
	assert.Equal(t, 2048, limits.MaxTokens)
	assert.Equal(t, 30, limits.TimeoutSeconds)
}

func TestResolveStepLimits_PartialStepOverride(t *testing.T) {
	// Step overrides only MaxLLMCalls; the rest come from agent config.
	store := state.NewInMemoryStore(zap.NewNop())
	reg := newTestRegistry(t)
	registerAgentWithLimits(t, reg, "custom-agent", 8, 16384, 120)

	sched := newTestSchedulerWithRegistry(t, store, &mockExecutor{}, reg, t.TempDir())
	step := planner.Step{
		AgentID:     "custom-agent",
		MaxLLMCalls: 2,
	}

	limits := sched.resolveStepLimits(context.Background(), step)

	assert.Equal(t, 2, limits.MaxLLMCalls, "step override")
	assert.Equal(t, 16384, limits.MaxTokens, "from agent config")
	assert.Equal(t, 120, limits.TimeoutSeconds, "from agent config")
}

func TestResolveStepLimits_PartialAgentOverride(t *testing.T) {
	// Agent config sets only TimeoutSeconds; rest fall to system defaults.
	store := state.NewInMemoryStore(zap.NewNop())
	reg := newTestRegistry(t)
	registerAgentWithLimits(t, reg, "partial-agent", 0, 0, 300)

	sched := newTestSchedulerWithRegistry(t, store, &mockExecutor{}, reg, t.TempDir())
	step := planner.Step{AgentID: "partial-agent"}

	limits := sched.resolveStepLimits(context.Background(), step)

	assert.Equal(t, 5, limits.MaxLLMCalls, "system default")
	assert.Equal(t, 8192, limits.MaxTokens, "system default")
	assert.Equal(t, 300, limits.TimeoutSeconds, "agent override")
}

func TestResolveStepLimits_AgentNotInRegistry(t *testing.T) {
	// Agent not found in registry → system defaults used (with warning log).
	store := state.NewInMemoryStore(zap.NewNop())
	reg := newTestRegistry(t) // empty registry

	sched := newTestSchedulerWithRegistry(t, store, &mockExecutor{}, reg, t.TempDir())
	step := planner.Step{AgentID: "unknown-agent"}

	limits := sched.resolveStepLimits(context.Background(), step)

	assert.Equal(t, 5, limits.MaxLLMCalls)
	assert.Equal(t, 8192, limits.MaxTokens)
	assert.Equal(t, 60, limits.TimeoutSeconds)
}

func TestResolveStepLimits_NilRegistry(t *testing.T) {
	// nil registry (e.g., in tests using newTestScheduler which passes nil)
	// → system defaults used, no panic.
	store := state.NewInMemoryStore(zap.NewNop())
	sched := newTestScheduler(t, store, &mockExecutor{}, t.TempDir())
	step := planner.Step{AgentID: "any-agent"}

	limits := sched.resolveStepLimits(context.Background(), step)

	assert.Equal(t, 5, limits.MaxLLMCalls)
	assert.Equal(t, 8192, limits.MaxTokens)
	assert.Equal(t, 60, limits.TimeoutSeconds)
}

func TestResolveStepLimits_EndToEnd(t *testing.T) {
	// Full end-to-end: workflow with step limits dispatches correct TaskConfig.
	const limitsYAML = `schema_version: "0.1"
workflow:
  id: "limits-wf"
  name: "Limits Workflow"
  trigger: "manual"
  steps:
    - id: "s1"
      agent: "test-agent"
      input: "do work"
      output_key: "result"
      max_llm_calls: 3
      max_tokens: 2048
      timeout_seconds: 45
`
	dir := t.TempDir()
	writeWorkflow(t, dir, "limits-wf", limitsYAML)

	store := state.NewInMemoryStore(zap.NewNop())
	var receivedLimits executor.StepLimits
	exec := &mockExecutor{handler: func(_ context.Context, req executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		receivedLimits = req.Limits
		return &executor.ExecuteResult{TaskID: "t1", Output: "ok"}, nil
	}}

	reg := newTestRegistry(t)
	registerAgentWithLimits(t, reg, "test-agent", 10, 16384, 300)

	sched := newTestSchedulerWithRegistry(t, store, exec, reg, dir, WithPollInterval(50*time.Millisecond))
	createPendingRun(t, store, "limits-run", "limits-wf", nil)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = sched.Run(ctx) }()

	waitForRunStatus(t, store, "limits-run", state.RunCompleted, 5*time.Second)

	assert.Equal(t, 3, receivedLimits.MaxLLMCalls, "step config overrides agent")
	assert.Equal(t, 2048, receivedLimits.MaxTokens, "step config overrides agent")
	assert.Equal(t, 45, receivedLimits.TimeoutSeconds, "step config overrides agent")
}

// --- Test helpers for resolveStepLimits ---

func newTestRegistry(t *testing.T) *registry.InMemoryRegistry {
	t.Helper()
	return registry.NewInMemoryRegistry(zap.NewNop())
}

func registerAgentWithLimits(t *testing.T, reg *registry.InMemoryRegistry, agentID string, maxLLMCalls, maxTokens, timeoutSeconds int) {
	t.Helper()
	err := reg.Register(context.Background(), registry.AgentInfo{
		ID:             agentID,
		Name:           agentID,
		Address:        "passthrough:///test",
		Status:         registry.StatusHealthy,
		MaxLLMCalls:    maxLLMCalls,
		MaxTokens:      maxTokens,
		TimeoutSeconds: timeoutSeconds,
	})
	require.NoError(t, err)
}

func newTestSchedulerWithRegistry(t *testing.T, store state.Store, exec executor.Executor, reg registry.Registry, workflowsDir string, opts ...Option) *WorkflowScheduler {
	t.Helper()
	logger := zap.NewNop()
	plan := planner.NewYAMLPlanner(logger)
	return NewWorkflowScheduler(store, reg, plan, exec, logger, workflowsDir, opts...)
}

// --- PR 3b: Budget integration tests ---

func testCostConfig() *cost.CostConfig {
	return &cost.CostConfig{
		Pricing: map[string]cost.ModelPricing{
			"claude-sonnet": {
				InputPer1MTokens:  3.00,
				OutputPer1MTokens: 15.00,
			},
		},
		Budgets: cost.BudgetThresholds{
			Global:      cost.GlobalBudget{MaxDailyUSD: 100.00, OnExceed: "fail"},
			PerWorkflow: cost.PerWorkflowBudget{DefaultMaxUSD: 10.00},
			PerAgent:    cost.PerAgentBudget{DefaultMaxUSD: 5.00},
		},
	}
}

func TestBudgetCheck_UnderBudget_DispatchProceeds(t *testing.T) {
	dir := t.TempDir()
	writeWorkflow(t, dir, "test-wf", singleStepYAML)

	cfg := testCostConfig()
	tc := cost.NewTokenCounter(cfg, zap.NewNop())
	be := cost.NewBudgetEnforcer(tc, cfg, zap.NewNop())
	cr := cost.NewCostReporter(tc, cfg, zap.NewNop())

	store := state.NewInMemoryStore(zap.NewNop())
	exec := &mockExecutor{handler: func(_ context.Context, req executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		return &executor.ExecuteResult{
			TaskID: "t1",
			Output: "ok",
			Metadata: map[string]string{
				"input_tokens":  "100",
				"output_tokens": "50",
				"model":         "claude-sonnet",
			},
		}, nil
	}}

	sched := newTestScheduler(t, store, exec, dir,
		WithPollInterval(50*time.Millisecond),
		WithCostComponents(tc, be, cr),
	)
	createPendingRun(t, store, "budget-ok", "test-wf", nil)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = sched.Run(ctx) }()

	run := waitForRunStatus(t, store, "budget-ok", state.RunCompleted, 5*time.Second)
	assert.Equal(t, state.RunCompleted, run.Status)

	// Verify tokens were recorded.
	input, output, _ := tc.GlobalUsage()
	assert.Equal(t, int64(100), input)
	assert.Equal(t, int64(50), output)
}

func TestBudgetCheck_Rejected_StepFails(t *testing.T) {
	dir := t.TempDir()
	writeWorkflow(t, dir, "test-wf", singleStepYAML)

	cfg := testCostConfig()
	// Set a tiny per-agent budget that will be exceeded by the estimated cost.
	cfg.Budgets.PerAgent.DefaultMaxUSD = 0.00001

	// Verify the budget check rejects directly (sanity check).
	tc := cost.NewTokenCounter(cfg, zap.NewNop())
	be := cost.NewBudgetEnforcer(tc, cfg, zap.NewNop())
	directResult := be.CheckBudget("test-wf", "test-agent", "claude-sonnet", 8192)
	require.Equal(t, cost.BudgetReject, directResult.Decision, "sanity: direct budget check should reject")

	cr := cost.NewCostReporter(tc, cfg, zap.NewNop())

	store := state.NewInMemoryStore(zap.NewNop())
	var executorCalled atomic.Bool
	exec := &mockExecutor{handler: func(_ context.Context, req executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		executorCalled.Store(true)
		return &executor.ExecuteResult{TaskID: "t1", Output: "ok"}, nil
	}}

	reg := newTestRegistry(t)
	err := reg.Register(context.Background(), registry.AgentInfo{
		ID:      "test-agent",
		Name:    "test-agent",
		Address: "passthrough:///test",
		Status:  registry.StatusHealthy,
		Model:   "claude-sonnet",
	})
	require.NoError(t, err)

	sched := newTestSchedulerWithRegistry(t, store, exec, reg, dir,
		WithPollInterval(50*time.Millisecond),
		WithCostComponents(tc, be, cr),
	)
	createPendingRun(t, store, "budget-fail", "test-wf", nil)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = sched.Run(ctx) }()

	run := waitForRunStatus(t, store, "budget-fail", state.RunFailed, 5*time.Second)
	assert.Equal(t, state.RunFailed, run.Status)
	assert.Contains(t, run.Error, "budget exceeded")
	assert.False(t, executorCalled.Load(), "executor should not be called when budget is rejected")

	// Step should also be marked as failed.
	step, ok := run.Steps["step1"]
	require.True(t, ok)
	assert.Equal(t, state.RunFailed, step.Status)
	assert.Contains(t, step.Error, "budget exceeded")
}

func TestBudgetCheck_ErrorWrapping(t *testing.T) {
	// Verify that budget rejection errors wrap ErrBudgetExceeded, enabling
	// programmatic detection via errors.Is without string matching.
	dir := t.TempDir()
	writeWorkflow(t, dir, "test-wf", singleStepYAML)

	cfg := testCostConfig()
	cfg.Budgets.PerAgent.DefaultMaxUSD = 0.00001

	tc := cost.NewTokenCounter(cfg, zap.NewNop())
	be := cost.NewBudgetEnforcer(tc, cfg, zap.NewNop())
	cr := cost.NewCostReporter(tc, cfg, zap.NewNop())

	store := state.NewInMemoryStore(zap.NewNop())
	exec := &mockExecutor{handler: func(_ context.Context, req executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		return &executor.ExecuteResult{TaskID: "t1", Output: "ok"}, nil
	}}

	reg := newTestRegistry(t)
	require.NoError(t, reg.Register(context.Background(), registry.AgentInfo{
		ID: "test-agent", Name: "test-agent", Address: "passthrough:///test",
		Status: registry.StatusHealthy, Model: "claude-sonnet",
	}))

	sched := newTestSchedulerWithRegistry(t, store, exec, reg, dir,
		WithPollInterval(50*time.Millisecond),
		WithCostComponents(tc, be, cr),
	)

	// Call executeStep directly to inspect the returned error.
	createPendingRun(t, store, "sentinel-run", "test-wf", nil)
	step := planner.Step{ID: "step1", AgentID: "test-agent", Input: "do something"}
	var mu sync.Mutex
	outputs := map[string]string{}
	vars := map[string]string{}

	_, err := sched.executeStep(context.Background(), "sentinel-run", "test-wf", step, outputs, vars, &mu)
	require.Error(t, err)
	assert.True(t, errors.Is(err, ErrBudgetExceeded), "error should wrap ErrBudgetExceeded sentinel")
	assert.Contains(t, err.Error(), "budget exceeded")
	assert.Contains(t, err.Error(), "per_agent budget exceeded")
}

func TestTokenRecording_MissingMetadata_GracefulDegradation(t *testing.T) {
	dir := t.TempDir()
	writeWorkflow(t, dir, "test-wf", singleStepYAML)

	cfg := testCostConfig()
	tc := cost.NewTokenCounter(cfg, zap.NewNop())
	be := cost.NewBudgetEnforcer(tc, cfg, zap.NewNop())
	cr := cost.NewCostReporter(tc, cfg, zap.NewNop())

	store := state.NewInMemoryStore(zap.NewNop())
	exec := &mockExecutor{handler: func(_ context.Context, req executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		// No token metadata in response.
		return &executor.ExecuteResult{TaskID: "t1", Output: "ok"}, nil
	}}

	sched := newTestScheduler(t, store, exec, dir,
		WithPollInterval(50*time.Millisecond),
		WithCostComponents(tc, be, cr),
	)
	createPendingRun(t, store, "no-meta", "test-wf", nil)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = sched.Run(ctx) }()

	run := waitForRunStatus(t, store, "no-meta", state.RunCompleted, 5*time.Second)
	assert.Equal(t, state.RunCompleted, run.Status)

	// Tokens should be zero (graceful degradation, no panic).
	input, output, _ := tc.GlobalUsage()
	assert.Equal(t, int64(0), input)
	assert.Equal(t, int64(0), output)
}

func TestTokenRecording_TokensUsedFallback(t *testing.T) {
	dir := t.TempDir()
	writeWorkflow(t, dir, "test-wf", singleStepYAML)

	cfg := testCostConfig()
	tc := cost.NewTokenCounter(cfg, zap.NewNop())
	be := cost.NewBudgetEnforcer(tc, cfg, zap.NewNop())
	cr := cost.NewCostReporter(tc, cfg, zap.NewNop())

	store := state.NewInMemoryStore(zap.NewNop())
	exec := &mockExecutor{handler: func(_ context.Context, req executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		return &executor.ExecuteResult{
			TaskID: "t1",
			Output: "ok",
			Metadata: map[string]string{
				"tokens_used": "500",
			},
		}, nil
	}}

	sched := newTestScheduler(t, store, exec, dir,
		WithPollInterval(50*time.Millisecond),
		WithCostComponents(tc, be, cr),
	)
	createPendingRun(t, store, "fallback-meta", "test-wf", nil)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = sched.Run(ctx) }()

	run := waitForRunStatus(t, store, "fallback-meta", state.RunCompleted, 5*time.Second)
	assert.Equal(t, state.RunCompleted, run.Status)

	// tokens_used should be recorded as output tokens (fallback path).
	_, output, _ := tc.GlobalUsage()
	assert.Equal(t, int64(500), output)
}

func TestCostReporter_StepCostRecorded(t *testing.T) {
	dir := t.TempDir()
	writeWorkflow(t, dir, "test-wf", singleStepYAML)

	cfg := testCostConfig()
	tc := cost.NewTokenCounter(cfg, zap.NewNop())
	be := cost.NewBudgetEnforcer(tc, cfg, zap.NewNop())
	cr := cost.NewCostReporter(tc, cfg, zap.NewNop())

	store := state.NewInMemoryStore(zap.NewNop())
	exec := &mockExecutor{handler: func(_ context.Context, req executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		return &executor.ExecuteResult{
			TaskID: "t1",
			Output: "ok",
			Metadata: map[string]string{
				"input_tokens":  "1000",
				"output_tokens": "500",
				"model":         "claude-sonnet",
			},
		}, nil
	}}

	sched := newTestScheduler(t, store, exec, dir,
		WithPollInterval(50*time.Millisecond),
		WithCostComponents(tc, be, cr),
	)
	createPendingRun(t, store, "reporter-run", "test-wf", nil)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = sched.Run(ctx) }()

	waitForRunStatus(t, store, "reporter-run", state.RunCompleted, 5*time.Second)

	// CostReporter should have step-level data.
	summary := cr.WorkflowSummary("test-wf")
	require.Len(t, summary.Steps, 1)
	assert.Equal(t, "step1", summary.Steps[0].StepID)
	assert.Equal(t, int64(1000), summary.Steps[0].InputTokens)
	assert.Equal(t, int64(500), summary.Steps[0].OutputTokens)
	assert.Equal(t, "claude-sonnet", summary.Steps[0].Model)
	assert.Greater(t, summary.Steps[0].EstimatedUSD, 0.0)
}

func TestNoCostComponents_NoPanic(t *testing.T) {
	// When cost components are nil (not injected), scheduler should work normally.
	dir := t.TempDir()
	writeWorkflow(t, dir, "test-wf", singleStepYAML)

	store := state.NewInMemoryStore(zap.NewNop())
	exec := &mockExecutor{handler: func(_ context.Context, req executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		return &executor.ExecuteResult{TaskID: "t1", Output: "ok"}, nil
	}}

	// No WithCostComponents — all cost fields remain nil.
	sched := newTestScheduler(t, store, exec, dir, WithPollInterval(50*time.Millisecond))
	createPendingRun(t, store, "no-cost", "test-wf", nil)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = sched.Run(ctx) }()

	run := waitForRunStatus(t, store, "no-cost", state.RunCompleted, 5*time.Second)
	assert.Equal(t, state.RunCompleted, run.Status)
}

func TestParseMetadataInt64_MalformedValue(t *testing.T) {
	core, logs := observer.New(zap.WarnLevel)
	logger := zap.New(core)

	metadata := map[string]string{
		"input_tokens": "not-a-number",
	}

	result := parseMetadataInt64(metadata, "input_tokens", logger, "step-x")
	assert.Equal(t, int64(0), result)

	// Verify warning log was emitted.
	require.Equal(t, 1, logs.Len())
	entry := logs.All()[0]
	assert.Equal(t, zap.WarnLevel, entry.Level)
	assert.Contains(t, entry.Message, "failed to parse metadata value")
	assert.Equal(t, "step-x", entry.ContextMap()["stepID"])
	assert.Equal(t, "input_tokens", entry.ContextMap()["key"])
	assert.Equal(t, "not-a-number", entry.ContextMap()["value"])
}

// --- PR #86 review: additional coverage tests ---

// TestResolveAgentModel_RegistryError verifies graceful degradation when the
// registry is non-nil but Get() returns an error (e.g., agent deregistered
// between scheduling and dispatch). resolveAgentModel should return "" and
// the budget check should proceed with a zero-cost estimate (no panic).
// (PR #86 review S-01)
func TestResolveAgentModel_RegistryError(t *testing.T) {
	dir := t.TempDir()
	writeWorkflow(t, dir, "test-wf", singleStepYAML)

	cfg := testCostConfig()
	tc := cost.NewTokenCounter(cfg, zap.NewNop())
	be := cost.NewBudgetEnforcer(tc, cfg, zap.NewNop())
	cr := cost.NewCostReporter(tc, cfg, zap.NewNop())

	store := state.NewInMemoryStore(zap.NewNop())
	exec := &mockExecutor{handler: func(_ context.Context, req executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		return &executor.ExecuteResult{
			TaskID: "t1",
			Output: "ok",
			Metadata: map[string]string{
				"input_tokens":  "100",
				"output_tokens": "50",
				"model":         "claude-sonnet",
			},
		}, nil
	}}

	// Register agent WITHOUT the "test-agent" ID used in singleStepYAML.
	// The registry is non-nil, but Get("test-agent") will return ErrAgentNotFound.
	reg := newTestRegistry(t)
	require.NoError(t, reg.Register(context.Background(), registry.AgentInfo{
		ID: "other-agent", Name: "other-agent", Address: "passthrough:///test",
		Status: registry.StatusHealthy, Model: "claude-sonnet",
	}))

	sched := newTestSchedulerWithRegistry(t, store, exec, reg, dir,
		WithPollInterval(50*time.Millisecond),
		WithCostComponents(tc, be, cr),
	)
	createPendingRun(t, store, "reg-error", "test-wf", nil)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = sched.Run(ctx) }()

	// Should complete — budget check passes with zero-cost estimate for unknown agent.
	run := waitForRunStatus(t, store, "reg-error", state.RunCompleted, 5*time.Second)
	assert.Equal(t, state.RunCompleted, run.Status)

	// Tokens should still be recorded (model comes from response metadata, not registry).
	input, output, _ := tc.GlobalUsage()
	assert.Equal(t, int64(100), input)
	assert.Equal(t, int64(50), output)
}

// budgetDepletionYAML defines a two-stage sequential workflow where step 2
// depends on step 1. Used to test that budget exhaustion in stage 1 causes
// stage 2 to be rejected by the budget enforcer.
const budgetDepletionYAML = `schema_version: "0.1"
workflow:
  id: "budget-depletion"
  name: "Budget Depletion"
  trigger: "manual"
  steps:
    - id: "step1"
      agent: "test-agent"
      input: "first task"
      output_key: "out1"
    - id: "step2"
      agent: "test-agent"
      input: "{{ steps.out1.output }}"
      output_key: "out2"
      depends_on: ["step1"]
`

// TestMultiStepBudgetDepletion validates the full budget lifecycle:
// step 1 dispatches and records enough token usage to exhaust the per-workflow
// budget, then step 2 (in a later stage) is rejected by the budget enforcer.
// (PR #86 review S-02)
func TestMultiStepBudgetDepletion(t *testing.T) {
	dir := t.TempDir()
	writeWorkflow(t, dir, "budget-depletion", budgetDepletionYAML)

	cfg := testCostConfig()
	// Set a per-workflow budget that step 1's token usage will exhaust.
	// Step 1 reports 100k output tokens on claude-sonnet ($15/1M output):
	// cost = 100_000 / 1_000_000 * 15.00 = $1.50
	// Per-workflow budget: $1.00 — so step 2 should be rejected.
	cfg.Budgets.PerWorkflow.DefaultMaxUSD = 1.00

	tc := cost.NewTokenCounter(cfg, zap.NewNop())
	be := cost.NewBudgetEnforcer(tc, cfg, zap.NewNop())
	cr := cost.NewCostReporter(tc, cfg, zap.NewNop())

	store := state.NewInMemoryStore(zap.NewNop())
	var stepCalls atomic.Int64
	exec := &mockExecutor{handler: func(_ context.Context, req executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		call := stepCalls.Add(1)
		if call == 1 {
			// Step 1: return a large token count that exhausts the budget.
			return &executor.ExecuteResult{
				TaskID: "t1",
				Output: "step1-done",
				Metadata: map[string]string{
					"input_tokens":  "10000",
					"output_tokens": "100000",
					"model":         "claude-sonnet",
				},
			}, nil
		}
		// Step 2 should never reach the executor.
		return &executor.ExecuteResult{TaskID: "t2", Output: "step2-done"}, nil
	}}

	reg := newTestRegistry(t)
	require.NoError(t, reg.Register(context.Background(), registry.AgentInfo{
		ID: "test-agent", Name: "test-agent", Address: "passthrough:///test",
		Status: registry.StatusHealthy, Model: "claude-sonnet",
	}))

	sched := newTestSchedulerWithRegistry(t, store, exec, reg, dir,
		WithPollInterval(50*time.Millisecond),
		WithCostComponents(tc, be, cr),
	)
	createPendingRun(t, store, "depletion-run", "budget-depletion", nil)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = sched.Run(ctx) }()

	run := waitForRunStatus(t, store, "depletion-run", state.RunFailed, 5*time.Second)
	assert.Equal(t, state.RunFailed, run.Status)
	assert.Contains(t, run.Error, "budget exceeded")

	// Step 1 should have completed, step 2 should have been rejected.
	assert.Equal(t, int64(1), stepCalls.Load(), "only step 1 should reach executor")

	step1, ok := run.Steps["step1"]
	require.True(t, ok)
	assert.Equal(t, state.RunCompleted, step1.Status)

	step2, ok := run.Steps["step2"]
	require.True(t, ok)
	assert.Equal(t, state.RunFailed, step2.Status)
	assert.Contains(t, step2.Error, "budget exceeded")

	// CostReporter should have step-level data only for step 1.
	summary := cr.WorkflowSummary("budget-depletion")
	require.Len(t, summary.Steps, 1)
	assert.Equal(t, "step1", summary.Steps[0].StepID)
}

// TestModelResolutionFallback_MetadataEmpty_RegistrySucceeds validates the
// model resolution fallback path in recordStepUsage: when executor response
// metadata has no "model" key but the registry has a model configured for
// the agent, the registry model is used for cost estimation.
// (PR #86 review S-03)
func TestModelResolutionFallback_MetadataEmpty_RegistrySucceeds(t *testing.T) {
	dir := t.TempDir()
	writeWorkflow(t, dir, "test-wf", singleStepYAML)

	cfg := testCostConfig()
	tc := cost.NewTokenCounter(cfg, zap.NewNop())
	be := cost.NewBudgetEnforcer(tc, cfg, zap.NewNop())
	cr := cost.NewCostReporter(tc, cfg, zap.NewNop())

	store := state.NewInMemoryStore(zap.NewNop())
	exec := &mockExecutor{handler: func(_ context.Context, req executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		return &executor.ExecuteResult{
			TaskID: "t1",
			Output: "ok",
			Metadata: map[string]string{
				// No "model" key — forces fallback to registry.
				"input_tokens":  "1000",
				"output_tokens": "500",
			},
		}, nil
	}}

	reg := newTestRegistry(t)
	require.NoError(t, reg.Register(context.Background(), registry.AgentInfo{
		ID: "test-agent", Name: "test-agent", Address: "passthrough:///test",
		Status: registry.StatusHealthy, Model: "claude-sonnet",
	}))

	sched := newTestSchedulerWithRegistry(t, store, exec, reg, dir,
		WithPollInterval(50*time.Millisecond),
		WithCostComponents(tc, be, cr),
	)
	createPendingRun(t, store, "fallback-model", "test-wf", nil)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = sched.Run(ctx) }()

	waitForRunStatus(t, store, "fallback-model", state.RunCompleted, 5*time.Second)

	// CostReporter step entry should use the registry model ("claude-sonnet").
	summary := cr.WorkflowSummary("test-wf")
	require.Len(t, summary.Steps, 1)
	assert.Equal(t, "claude-sonnet", summary.Steps[0].Model, "model should come from registry fallback")
	assert.Equal(t, int64(1000), summary.Steps[0].InputTokens)
	assert.Equal(t, int64(500), summary.Steps[0].OutputTokens)
	assert.Greater(t, summary.Steps[0].EstimatedUSD, 0.0, "cost should be non-zero with priced model")
}

// TestParseMetadataInt64_NegativeValue verifies that negative token values are
// clamped to zero with a warning log. This prevents adversarial agents from
// reporting negative tokens to decrease the running budget total.
// (PR #86 review F-01: security fix)
func TestParseMetadataInt64_NegativeValue(t *testing.T) {
	core, logs := observer.New(zap.WarnLevel)
	logger := zap.New(core)

	metadata := map[string]string{
		"input_tokens": "-500",
	}

	result := parseMetadataInt64(metadata, "input_tokens", logger, "step-neg")
	assert.Equal(t, int64(0), result)

	// Verify warning log was emitted for the clamped value.
	require.Equal(t, 1, logs.Len())
	entry := logs.All()[0]
	assert.Equal(t, zap.WarnLevel, entry.Level)
	assert.Contains(t, entry.Message, "negative token value clamped to zero")
	assert.Equal(t, "step-neg", entry.ContextMap()["stepID"])
	assert.Equal(t, "input_tokens", entry.ContextMap()["key"])
	assert.Equal(t, int64(-500), entry.ContextMap()["value"])
}

// TestUnpricedModel_DebugLog validates the S-04 diagnostic log: when a step
// response contains a model not in the pricing table with non-zero tokens,
// a Debug-level log is emitted with "model not in pricing table". This prevents
// accidental removal of the observability signal that helps operators detect
// unpriced models. (PR #86 review S-04)
func TestUnpricedModel_DebugLog(t *testing.T) {
	dir := t.TempDir()
	writeWorkflow(t, dir, "test-wf", singleStepYAML)

	cfg := testCostConfig()
	tc := cost.NewTokenCounter(cfg, zap.NewNop())
	be := cost.NewBudgetEnforcer(tc, cfg, zap.NewNop())

	// Use DebugLevel observer on the scheduler logger to capture Debug-level logs.
	core, logs := observer.New(zap.DebugLevel)
	schedLogger := zap.New(core)

	cr := cost.NewCostReporter(tc, cfg, zap.NewNop())

	store := state.NewInMemoryStore(zap.NewNop())
	exec := &mockExecutor{handler: func(_ context.Context, req executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		return &executor.ExecuteResult{
			TaskID: "t1",
			Output: "ok",
			Metadata: map[string]string{
				"input_tokens":  "1000",
				"output_tokens": "500",
				"model":         "unknown-model-xyz",
			},
		}, nil
	}}

	plan := planner.NewYAMLPlanner(zap.NewNop())
	sched := NewWorkflowScheduler(store, nil, plan, exec, schedLogger, dir,
		WithPollInterval(50*time.Millisecond),
		WithCostComponents(tc, be, cr),
	)
	createPendingRun(t, store, "unpriced-run", "test-wf", nil)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = sched.Run(ctx) }()

	waitForRunStatus(t, store, "unpriced-run", state.RunCompleted, 5*time.Second)

	// Find the Debug-level log about unpriced model.
	var found bool
	for _, entry := range logs.All() {
		if entry.Message == "model not in pricing table, step cost recorded as $0" {
			found = true
			assert.Equal(t, zap.DebugLevel, entry.Level)
			assert.Equal(t, "unknown-model-xyz", entry.ContextMap()["model"])
			assert.Equal(t, "step1", entry.ContextMap()["stepID"])
			break
		}
	}
	assert.True(t, found, "expected Debug log for unpriced model")

	// Step cost should be recorded with $0 for unknown model.
	summary := cr.WorkflowSummary("test-wf")
	require.Len(t, summary.Steps, 1)
	assert.Equal(t, 0.0, summary.Steps[0].EstimatedUSD)
	assert.Equal(t, "unknown-model-xyz", summary.Steps[0].Model)
}

// TestTokensUsedFallback_LogMessage validates that when the legacy "tokens_used"
// fallback path is taken, an Info-level log is emitted so operators can identify
// agents that need to provide granular input_tokens/output_tokens data.
// (PR #86 review: observability regression protection)
func TestTokensUsedFallback_LogMessage(t *testing.T) {
	dir := t.TempDir()
	writeWorkflow(t, dir, "test-wf", singleStepYAML)

	cfg := testCostConfig()
	tc := cost.NewTokenCounter(cfg, zap.NewNop())
	be := cost.NewBudgetEnforcer(tc, cfg, zap.NewNop())

	// Use InfoLevel observer to capture the fallback log.
	core, logs := observer.New(zap.InfoLevel)
	schedLogger := zap.New(core)

	cr := cost.NewCostReporter(tc, cfg, zap.NewNop())

	store := state.NewInMemoryStore(zap.NewNop())
	exec := &mockExecutor{handler: func(_ context.Context, req executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		return &executor.ExecuteResult{
			TaskID: "t1",
			Output: "ok",
			Metadata: map[string]string{
				"tokens_used": "750",
				"model":       "claude-sonnet",
			},
		}, nil
	}}

	plan := planner.NewYAMLPlanner(zap.NewNop())
	sched := NewWorkflowScheduler(store, nil, plan, exec, schedLogger, dir,
		WithPollInterval(50*time.Millisecond),
		WithCostComponents(tc, be, cr),
	)
	createPendingRun(t, store, "fallback-log", "test-wf", nil)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = sched.Run(ctx) }()

	waitForRunStatus(t, store, "fallback-log", state.RunCompleted, 5*time.Second)

	// Find the Info-level log about tokens_used fallback.
	var found bool
	for _, entry := range logs.All() {
		if entry.Message == "using tokens_used fallback (all tokens mapped to output, cost may be overestimated)" {
			found = true
			assert.Equal(t, zap.InfoLevel, entry.Level)
			assert.Equal(t, "step1", entry.ContextMap()["stepID"])
			assert.Equal(t, "test-agent", entry.ContextMap()["agentID"])
			assert.Equal(t, int64(750), entry.ContextMap()["tokensUsed"])
			break
		}
	}
	assert.True(t, found, "expected Info log for tokens_used fallback")
}

// --- StepExecutionMetadata tests (RFC 0006 PR 4a) ---

func TestStepMetadata_PopulatedOnCompletion(t *testing.T) {
	dir := t.TempDir()
	writeWorkflow(t, dir, "test-wf", singleStepYAML)

	store := state.NewInMemoryStore(zap.NewNop())
	exec := &mockExecutor{handler: func(_ context.Context, req executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		return &executor.ExecuteResult{
			TaskID:     "t1",
			Output:     "done",
			RetryCount: 2,
			WallTimeMs: 1500,
			Metadata: map[string]string{
				"tokens_used":    "800",
				"llm_call_count": "3",
			},
		}, nil
	}}

	sched := newTestScheduler(t, store, exec, dir,
		WithPollInterval(50*time.Millisecond),
	)
	createPendingRun(t, store, "meta-run", "test-wf", nil)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = sched.Run(ctx) }()

	run := waitForRunStatus(t, store, "meta-run", state.RunCompleted, 5*time.Second)

	step := run.Steps["step1"]
	require.NotNil(t, step.Metadata, "step should have execution metadata")
	assert.Equal(t, 800, step.Metadata.TokensUsed)
	assert.Equal(t, 3, step.Metadata.LLMCallCount)
	assert.Equal(t, 2, step.Metadata.RetryCount)
	assert.False(t, step.Metadata.CacheHit)
	assert.Equal(t, int64(1500), step.Metadata.WallTimeMs)
}

func TestStepMetadata_GracefulDegradation(t *testing.T) {
	dir := t.TempDir()
	writeWorkflow(t, dir, "test-wf", singleStepYAML)

	store := state.NewInMemoryStore(zap.NewNop())
	exec := &mockExecutor{handler: func(_ context.Context, req executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		// No metadata fields — simulates an agent that doesn't report observability data.
		return &executor.ExecuteResult{
			TaskID:     "t1",
			Output:     "done",
			WallTimeMs: 200,
		}, nil
	}}

	sched := newTestScheduler(t, store, exec, dir,
		WithPollInterval(50*time.Millisecond),
	)
	createPendingRun(t, store, "meta-empty", "test-wf", nil)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = sched.Run(ctx) }()

	run := waitForRunStatus(t, store, "meta-empty", state.RunCompleted, 5*time.Second)

	step := run.Steps["step1"]
	require.NotNil(t, step.Metadata, "step should have metadata even with no agent-reported fields")
	assert.Equal(t, 0, step.Metadata.TokensUsed)
	assert.Equal(t, 0, step.Metadata.LLMCallCount)
	assert.Equal(t, 0, step.Metadata.RetryCount)
	assert.False(t, step.Metadata.CacheHit)
	assert.Equal(t, int64(200), step.Metadata.WallTimeMs)
}

func TestStepMetadata_PerDirectionTokens(t *testing.T) {
	dir := t.TempDir()
	writeWorkflow(t, dir, "test-wf", singleStepYAML)

	store := state.NewInMemoryStore(zap.NewNop())
	exec := &mockExecutor{handler: func(_ context.Context, req executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		return &executor.ExecuteResult{
			TaskID:     "t1",
			Output:     "done",
			WallTimeMs: 500,
			Metadata: map[string]string{
				"input_tokens":  "300",
				"output_tokens": "200",
			},
		}, nil
	}}

	sched := newTestScheduler(t, store, exec, dir,
		WithPollInterval(50*time.Millisecond),
	)
	createPendingRun(t, store, "meta-dir", "test-wf", nil)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = sched.Run(ctx) }()

	run := waitForRunStatus(t, store, "meta-dir", state.RunCompleted, 5*time.Second)

	step := run.Steps["step1"]
	require.NotNil(t, step.Metadata)
	assert.Equal(t, 500, step.Metadata.TokensUsed, "should sum input + output tokens")
}

func TestStepMetadata_InfoLogOnCompletion(t *testing.T) {
	dir := t.TempDir()
	writeWorkflow(t, dir, "test-wf", singleStepYAML)

	store := state.NewInMemoryStore(zap.NewNop())
	exec := &mockExecutor{handler: func(_ context.Context, req executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		return &executor.ExecuteResult{
			TaskID:     "t1",
			Output:     "done",
			RetryCount: 1,
			WallTimeMs: 750,
			Metadata: map[string]string{
				"tokens_used": "500",
			},
		}, nil
	}}

	// Use observed logger to capture log output.
	core, logs := observer.New(zap.InfoLevel)
	logger := zap.New(core)
	plan := planner.NewYAMLPlanner(logger)
	sched := NewWorkflowScheduler(store, nil, plan, exec, logger, dir,
		WithPollInterval(50*time.Millisecond),
	)
	createPendingRun(t, store, "meta-log", "test-wf", nil)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = sched.Run(ctx) }()

	waitForRunStatus(t, store, "meta-log", state.RunCompleted, 5*time.Second)

	var found bool
	for _, entry := range logs.All() {
		if entry.Message == "step completed" {
			found = true
			assert.Equal(t, "step1", entry.ContextMap()["stepID"])
			assert.Equal(t, int64(500), entry.ContextMap()["tokensUsed"])
			assert.Equal(t, int64(1), entry.ContextMap()["retryCount"])
			assert.Equal(t, int64(750), entry.ContextMap()["wallTimeMs"])
			break
		}
	}
	assert.True(t, found, "expected 'step completed' Info log with metadata fields")
}

// TestStepMetadata_CostParity_TokensUsedOnly verifies that buildStepMetadata and
// recordStepUsage produce the same estimated cost when only tokens_used is
// reported (no input_tokens/output_tokens). This is the M-01 regression test:
// before the fix, buildStepMetadata called EstimateCost(model, 0, 0) → $0.00
// while recordStepUsage correctly mapped tokens_used → outputTokens.
func TestStepMetadata_CostParity_TokensUsedOnly(t *testing.T) {
	dir := t.TempDir()
	writeWorkflow(t, dir, "test-wf", singleStepYAML)

	cfg := &cost.CostConfig{
		Pricing: map[string]cost.ModelPricing{
			"claude-sonnet": {InputPer1MTokens: 3.00, OutputPer1MTokens: 15.00},
		},
		Budgets: cost.BudgetThresholds{
			Global:      cost.GlobalBudget{MaxDailyUSD: 100, OnExceed: "fail"},
			PerWorkflow: cost.PerWorkflowBudget{DefaultMaxUSD: 50},
			PerAgent:    cost.PerAgentBudget{DefaultMaxUSD: 25},
		},
	}
	tc := cost.NewTokenCounter(cfg, zap.NewNop())
	be := cost.NewBudgetEnforcer(tc, cfg, zap.NewNop())
	cr := cost.NewCostReporter(tc, cfg, zap.NewNop())

	store := state.NewInMemoryStore(zap.NewNop())
	exec := &mockExecutor{handler: func(_ context.Context, req executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		return &executor.ExecuteResult{
			TaskID:     "t1",
			Output:     "done",
			WallTimeMs: 500,
			Metadata: map[string]string{
				"tokens_used": "800",
				"model":       "claude-sonnet",
			},
		}, nil
	}}

	plan := planner.NewYAMLPlanner(zap.NewNop())
	sched := NewWorkflowScheduler(store, nil, plan, exec, zap.NewNop(), dir,
		WithPollInterval(50*time.Millisecond),
		WithCostComponents(tc, be, cr),
	)
	createPendingRun(t, store, "cost-parity", "test-wf", nil)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = sched.Run(ctx) }()

	run := waitForRunStatus(t, store, "cost-parity", state.RunCompleted, 5*time.Second)

	step := run.Steps["step1"]
	require.NotNil(t, step.Metadata, "step should have execution metadata")

	// The metadata cost should be non-zero: 800 tokens_used mapped to outputTokens,
	// priced at 15.00/1M = 800 * 15.00 / 1_000_000 = 0.012.
	assert.Greater(t, step.Metadata.EstimatedCostUSD, 0.0,
		"M-01: estimated cost should be non-zero when tokens_used > 0 and model has pricing")
	expectedCost := 800.0 * 15.00 / 1_000_000.0
	assert.InDelta(t, expectedCost, step.Metadata.EstimatedCostUSD, 0.0001,
		"metadata cost should match tokens_used → outputTokens pessimistic estimate")

	// Verify CostReporter recorded the same cost (parity check).
	report := cr.WorkflowSummary("test-wf")
	require.Len(t, report.Steps, 1, "should have one step cost entry")
	assert.InDelta(t, expectedCost, report.Steps[0].EstimatedUSD, 0.0001,
		"reporter cost should match metadata cost (parity)")
}

// TestResolveStepLimits_NegativeAgentLimits verifies that negative agent-level
// limits produce a warning log and fall through to system defaults. (PR 5a, F-04)
func TestResolveStepLimits_NegativeAgentLimits(t *testing.T) {
	core, logs := observer.New(zap.WarnLevel)
	logger := zap.New(core)

	reg := registry.NewInMemoryRegistry(zap.NewNop())
	require.NoError(t, reg.Register(context.Background(), registry.AgentInfo{
		ID:             "neg-agent",
		Name:           "neg-agent",
		Address:        "localhost:0",
		Status:         registry.StatusHealthy,
		MaxLLMCalls:    -5,
		MaxTokens:      -1000,
		TimeoutSeconds: -30,
	}))

	plan := planner.NewYAMLPlanner(zap.NewNop())
	store := state.NewInMemoryStore(zap.NewNop())
	exec := &mockExecutor{handler: func(_ context.Context, req executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		return &executor.ExecuteResult{TaskID: "t1", Output: "ok"}, nil
	}}
	sched := NewWorkflowScheduler(store, reg, plan, exec, logger, t.TempDir())

	limits := sched.resolveStepLimits(context.Background(), planner.Step{
		ID:      "s1",
		AgentID: "neg-agent",
	})

	// Negative values should fall through to defaults.
	assert.Equal(t, 5, limits.MaxLLMCalls, "should use default MaxLLMCalls")
	assert.Equal(t, 8192, limits.MaxTokens, "should use default MaxTokens")
	assert.Equal(t, 60, limits.TimeoutSeconds, "should use default TimeoutSeconds")

	// Verify warning logs were emitted.
	warnMessages := make(map[string]bool)
	for _, entry := range logs.All() {
		warnMessages[entry.Message] = true
	}
	assert.True(t, warnMessages["negative agent-level MaxLLMCalls, using default"],
		"expected warning for negative MaxLLMCalls")
	assert.True(t, warnMessages["negative agent-level MaxTokens, using default"],
		"expected warning for negative MaxTokens")
	assert.True(t, warnMessages["negative agent-level TimeoutSeconds, using default"],
		"expected warning for negative TimeoutSeconds")
}
