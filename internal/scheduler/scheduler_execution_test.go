package scheduler

import (
	"context"
	"fmt"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/executor"
	"github.com/mkhomutov/persatrix/internal/state"
)

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
