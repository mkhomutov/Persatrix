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
	"go.uber.org/zap/zaptest/observer"

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
