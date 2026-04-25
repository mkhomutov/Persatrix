package scheduler

import (
	"context"
	"fmt"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/executor"
	"github.com/mkhomutov/persatrix/internal/state"
)

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
