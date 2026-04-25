package state

import (
	"context"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestCRUDFullLifecycle(t *testing.T) {
	ctx := context.Background()
	store := newTestStore(t)

	// Create.
	run := &WorkflowRun{
		ID:         "lc-1",
		WorkflowID: "feature-builder",
		Status:     RunPending,
		Inputs:     map[string]string{"user_request": "build login page"},
	}
	require.NoError(t, store.CreateRun(ctx, run))

	// Update status to running.
	require.NoError(t, store.UpdateRunStatus(ctx, "lc-1", RunRunning))

	// Add step states.
	require.NoError(t, store.UpdateStepState(ctx, "lc-1", StepState{
		StepID:    "plan",
		Status:    RunCompleted,
		Output:    "plan output",
		StartedAt: time.Now(),
	}))
	require.NoError(t, store.UpdateStepState(ctx, "lc-1", StepState{
		StepID:    "implement",
		Status:    RunRunning,
		StartedAt: time.Now(),
	}))

	// Verify intermediate state.
	got, err := store.GetRun(ctx, "lc-1")
	require.NoError(t, err)
	assert.Equal(t, RunRunning, got.Status)
	assert.Equal(t, RunCompleted, got.Steps["plan"].Status)
	assert.Equal(t, RunRunning, got.Steps["implement"].Status)

	// Complete workflow.
	require.NoError(t, store.UpdateRunStatus(ctx, "lc-1", RunCompleted))
	got, err = store.GetRun(ctx, "lc-1")
	require.NoError(t, err)
	assert.Equal(t, RunCompleted, got.Status)

	// List should contain our run.
	runs, err := store.ListRuns(ctx)
	require.NoError(t, err)
	assert.Len(t, runs, 1)

	// Delete.
	require.NoError(t, store.DeleteRun(ctx, "lc-1"))
	runs, err = store.ListRuns(ctx)
	require.NoError(t, err)
	assert.Empty(t, runs)
}

func TestRunStatusString(t *testing.T) {
	tests := []struct {
		status RunStatus
		want   string
	}{
		{RunPending, "Pending"},
		{RunRunning, "Running"},
		{RunCompleted, "Completed"},
		{RunFailed, "Failed"},
		{RunCancelled, "Cancelled"},
		{RunRetrying, "Retrying"},
		{RunStatus(99), "RunStatus(99)"},
	}
	for _, tt := range tests {
		assert.Equal(t, tt.want, tt.status.String())
	}
}

func TestRunRetryingStatus(t *testing.T) {
	ctx := context.Background()
	store := newTestStore(t)

	// RunRetrying = 5, explicit integer aligned with proto/task.proto RETRYING = 5.
	assert.Equal(t, RunStatus(5), RunRetrying)

	// Create a run with RunRetrying status and retrieve it.
	run := &WorkflowRun{ID: "retry-1", WorkflowID: "wf-1", Status: RunRetrying}
	require.NoError(t, store.CreateRun(ctx, run))

	got, err := store.GetRun(ctx, "retry-1")
	require.NoError(t, err)
	assert.Equal(t, RunRetrying, got.Status)
}

func TestSetRunTimestampsBoth(t *testing.T) {
	ctx := context.Background()
	store := newTestStore(t)

	require.NoError(t, store.CreateRun(ctx, &WorkflowRun{ID: "ts-1", WorkflowID: "wf-1"}))

	started := time.Date(2026, 4, 10, 12, 0, 0, 0, time.UTC)
	finished := time.Date(2026, 4, 10, 12, 5, 0, 0, time.UTC)

	err := store.SetRunTimestamps(ctx, "ts-1", &started, &finished)
	require.NoError(t, err)

	got, err := store.GetRun(ctx, "ts-1")
	require.NoError(t, err)
	assert.Equal(t, started, got.StartedAt)
	assert.Equal(t, finished, got.FinishedAt)
}

func TestSetRunTimestampsOnlyStartedAt(t *testing.T) {
	ctx := context.Background()
	store := newTestStore(t)

	require.NoError(t, store.CreateRun(ctx, &WorkflowRun{ID: "ts-2", WorkflowID: "wf-1"}))

	started := time.Date(2026, 4, 10, 12, 0, 0, 0, time.UTC)

	err := store.SetRunTimestamps(ctx, "ts-2", &started, nil)
	require.NoError(t, err)

	got, err := store.GetRun(ctx, "ts-2")
	require.NoError(t, err)
	assert.Equal(t, started, got.StartedAt)
	assert.True(t, got.FinishedAt.IsZero(), "FinishedAt should remain zero")
}

func TestSetRunTimestampsOnlyFinishedAt(t *testing.T) {
	ctx := context.Background()
	store := newTestStore(t)

	started := time.Date(2026, 4, 10, 11, 0, 0, 0, time.UTC)
	require.NoError(t, store.CreateRun(ctx, &WorkflowRun{
		ID:         "ts-3",
		WorkflowID: "wf-1",
		StartedAt:  started,
	}))

	finished := time.Date(2026, 4, 10, 12, 0, 0, 0, time.UTC)

	err := store.SetRunTimestamps(ctx, "ts-3", nil, &finished)
	require.NoError(t, err)

	got, err := store.GetRun(ctx, "ts-3")
	require.NoError(t, err)
	assert.Equal(t, started, got.StartedAt, "StartedAt should remain unchanged")
	assert.Equal(t, finished, got.FinishedAt)
}

func TestSetRunTimestampsNotFound(t *testing.T) {
	ctx := context.Background()
	store := newTestStore(t)

	started := time.Now()
	err := store.SetRunTimestamps(ctx, "nonexistent", &started, nil)
	assert.ErrorIs(t, err, ErrRunNotFound)
}

func TestSetRunError(t *testing.T) {
	ctx := context.Background()
	store := newTestStore(t)

	require.NoError(t, store.CreateRun(ctx, &WorkflowRun{ID: "err-1", WorkflowID: "wf-1"}))

	err := store.SetRunError(ctx, "err-1", "step 'plan' failed: agent timeout")
	require.NoError(t, err)

	got, err := store.GetRun(ctx, "err-1")
	require.NoError(t, err)
	assert.Equal(t, "step 'plan' failed: agent timeout", got.Error)
}

func TestSetRunErrorNotFound(t *testing.T) {
	ctx := context.Background()
	store := newTestStore(t)

	err := store.SetRunError(ctx, "nonexistent", "some error")
	assert.ErrorIs(t, err, ErrRunNotFound)
}

func TestRunStatusValues(t *testing.T) {
	// Verify explicit integer assignments match proto/task.proto alignment.
	assert.Equal(t, RunStatus(0), RunPending)
	assert.Equal(t, RunStatus(1), RunRunning)
	assert.Equal(t, RunStatus(2), RunCompleted)
	assert.Equal(t, RunStatus(3), RunFailed)
	assert.Equal(t, RunStatus(4), RunCancelled)
	assert.Equal(t, RunStatus(5), RunRetrying)
}
