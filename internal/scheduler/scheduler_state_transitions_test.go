package scheduler

import (
	"context"
	"fmt"
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
