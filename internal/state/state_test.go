package state

import (
	"context"
	"sync"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
)

func newTestStore(t *testing.T) *InMemoryStore {
	t.Helper()
	return NewInMemoryStore(zap.NewNop())
}

func TestCreateRun(t *testing.T) {
	ctx := context.Background()
	store := newTestStore(t)

	run := &WorkflowRun{
		ID:         "run-1",
		WorkflowID: "wf-1",
		Status:     RunPending,
		Inputs:     map[string]string{"user_request": "build a thing"},
	}

	err := store.CreateRun(ctx, run)
	require.NoError(t, err)

	got, err := store.GetRun(ctx, "run-1")
	require.NoError(t, err)
	assert.Equal(t, "run-1", got.ID)
	assert.Equal(t, "wf-1", got.WorkflowID)
	assert.Equal(t, RunPending, got.Status)
	assert.Equal(t, "build a thing", got.Inputs["user_request"])
}

func TestCreateRunGeneratesUUID(t *testing.T) {
	ctx := context.Background()
	store := newTestStore(t)

	run := &WorkflowRun{WorkflowID: "wf-1"}
	err := store.CreateRun(ctx, run)
	require.NoError(t, err)
	assert.NotEmpty(t, run.ID, "expected auto-generated UUID")
	assert.Len(t, run.ID, 36, "expected UUID format (36 chars with dashes)")
}

func TestCreateRunDuplicateReturnsError(t *testing.T) {
	ctx := context.Background()
	store := newTestStore(t)

	run := &WorkflowRun{ID: "dup-1", WorkflowID: "wf-1"}
	require.NoError(t, store.CreateRun(ctx, run))

	err := store.CreateRun(ctx, &WorkflowRun{ID: "dup-1", WorkflowID: "wf-2"})
	assert.ErrorIs(t, err, ErrRunAlreadyExists)
}

func TestCreateRunWithNilSteps(t *testing.T) {
	ctx := context.Background()
	store := newTestStore(t)

	run := &WorkflowRun{ID: "nil-steps", WorkflowID: "wf-1", Steps: nil}
	require.NoError(t, store.CreateRun(ctx, run))

	// UpdateStepState should work even though Steps was nil at creation.
	err := store.UpdateStepState(ctx, "nil-steps", StepState{StepID: "step-1", Status: RunRunning})
	require.NoError(t, err)

	got, err := store.GetRun(ctx, "nil-steps")
	require.NoError(t, err)
	assert.Equal(t, RunRunning, got.Steps["step-1"].Status)
}

func TestCreateRunWithEmptySteps(t *testing.T) {
	ctx := context.Background()
	store := newTestStore(t)

	run := &WorkflowRun{ID: "empty-steps", WorkflowID: "wf-1", Steps: map[string]StepState{}}
	require.NoError(t, store.CreateRun(ctx, run))

	err := store.UpdateStepState(ctx, "empty-steps", StepState{StepID: "step-1", Status: RunRunning})
	require.NoError(t, err)

	got, err := store.GetRun(ctx, "empty-steps")
	require.NoError(t, err)
	assert.Equal(t, RunRunning, got.Steps["step-1"].Status)
}

func TestCreateRunWithPrePopulatedSteps(t *testing.T) {
	ctx := context.Background()
	store := newTestStore(t)

	run := &WorkflowRun{
		ID:         "pre-pop",
		WorkflowID: "wf-1",
		Steps: map[string]StepState{
			"existing": {StepID: "existing", Status: RunPending},
		},
	}
	require.NoError(t, store.CreateRun(ctx, run))

	got, err := store.GetRun(ctx, "pre-pop")
	require.NoError(t, err)
	assert.Equal(t, RunPending, got.Steps["existing"].Status)
}

func TestGetRunNotFound(t *testing.T) {
	ctx := context.Background()
	store := newTestStore(t)

	_, err := store.GetRun(ctx, "nonexistent")
	assert.ErrorIs(t, err, ErrRunNotFound)
}

func TestGetRunDeepCopy(t *testing.T) {
	ctx := context.Background()
	store := newTestStore(t)

	run := &WorkflowRun{
		ID:         "dc-1",
		WorkflowID: "wf-1",
		Steps: map[string]StepState{
			"s1": {StepID: "s1", Status: RunPending},
		},
		Inputs: map[string]string{"key": "value"},
	}
	require.NoError(t, store.CreateRun(ctx, run))

	got, err := store.GetRun(ctx, "dc-1")
	require.NoError(t, err)

	// Mutate the returned copy.
	got.Status = RunFailed
	got.Steps["s1"] = StepState{StepID: "s1", Status: RunFailed}
	got.Steps["injected"] = StepState{StepID: "injected"}
	got.Inputs["key"] = "corrupted"
	got.Inputs["injected"] = "bad"

	// Original in store must be unaffected.
	original, err := store.GetRun(ctx, "dc-1")
	require.NoError(t, err)
	assert.Equal(t, RunPending, original.Status)
	assert.Equal(t, RunPending, original.Steps["s1"].Status)
	assert.NotContains(t, original.Steps, "injected")
	assert.Equal(t, "value", original.Inputs["key"])
	assert.NotContains(t, original.Inputs, "injected")
}

func TestCreateRunDeepCopy(t *testing.T) {
	ctx := context.Background()
	store := newTestStore(t)

	steps := map[string]StepState{
		"s1": {StepID: "s1", Status: RunPending},
	}
	inputs := map[string]string{"key": "value"}
	run := &WorkflowRun{
		ID:         "create-dc",
		WorkflowID: "wf-1",
		Steps:      steps,
		Inputs:     inputs,
	}
	require.NoError(t, store.CreateRun(ctx, run))

	// Mutate the original maps after creation.
	steps["s1"] = StepState{StepID: "s1", Status: RunFailed}
	steps["injected"] = StepState{StepID: "injected"}
	inputs["key"] = "corrupted"

	// Store must be unaffected.
	got, err := store.GetRun(ctx, "create-dc")
	require.NoError(t, err)
	assert.Equal(t, RunPending, got.Steps["s1"].Status)
	assert.NotContains(t, got.Steps, "injected")
	assert.Equal(t, "value", got.Inputs["key"])
}

func TestListRuns(t *testing.T) {
	ctx := context.Background()
	store := newTestStore(t)

	require.NoError(t, store.CreateRun(ctx, &WorkflowRun{ID: "r1", WorkflowID: "wf-1"}))
	require.NoError(t, store.CreateRun(ctx, &WorkflowRun{ID: "r2", WorkflowID: "wf-2"}))

	runs, err := store.ListRuns(ctx)
	require.NoError(t, err)
	assert.Len(t, runs, 2)

	ids := map[string]bool{}
	for _, r := range runs {
		ids[r.ID] = true
	}
	assert.True(t, ids["r1"])
	assert.True(t, ids["r2"])
}

func TestListRunsEmpty(t *testing.T) {
	ctx := context.Background()
	store := newTestStore(t)

	runs, err := store.ListRuns(ctx)
	require.NoError(t, err)
	assert.Empty(t, runs)
}

func TestListRunsDeepCopy(t *testing.T) {
	ctx := context.Background()
	store := newTestStore(t)

	require.NoError(t, store.CreateRun(ctx, &WorkflowRun{
		ID:         "ldc-1",
		WorkflowID: "wf-1",
		Steps:      map[string]StepState{"s1": {StepID: "s1", Status: RunPending}},
		Inputs:     map[string]string{"key": "value"},
	}))

	runs, err := store.ListRuns(ctx)
	require.NoError(t, err)
	require.Len(t, runs, 1)

	// Mutate the returned list entry.
	runs[0].Status = RunFailed
	runs[0].Steps["s1"] = StepState{StepID: "s1", Status: RunFailed}
	runs[0].Inputs["key"] = "corrupted"

	// Store must be unaffected.
	original, err := store.GetRun(ctx, "ldc-1")
	require.NoError(t, err)
	assert.Equal(t, RunPending, original.Status)
	assert.Equal(t, RunPending, original.Steps["s1"].Status)
	assert.Equal(t, "value", original.Inputs["key"])
}

func TestUpdateRunStatus(t *testing.T) {
	ctx := context.Background()
	store := newTestStore(t)

	require.NoError(t, store.CreateRun(ctx, &WorkflowRun{ID: "us-1", WorkflowID: "wf-1"}))

	err := store.UpdateRunStatus(ctx, "us-1", RunRunning)
	require.NoError(t, err)

	got, err := store.GetRun(ctx, "us-1")
	require.NoError(t, err)
	assert.Equal(t, RunRunning, got.Status)
}

func TestUpdateRunStatusNotFound(t *testing.T) {
	ctx := context.Background()
	store := newTestStore(t)

	err := store.UpdateRunStatus(ctx, "nonexistent", RunRunning)
	assert.ErrorIs(t, err, ErrRunNotFound)
}

func TestUpdateRunStatusNoTransitionValidation(t *testing.T) {
	ctx := context.Background()
	store := newTestStore(t)

	require.NoError(t, store.CreateRun(ctx, &WorkflowRun{ID: "ntv-1", WorkflowID: "wf-1"}))
	require.NoError(t, store.UpdateRunStatus(ctx, "ntv-1", RunCompleted))

	// Backwards transition should be allowed in v0.1.
	err := store.UpdateRunStatus(ctx, "ntv-1", RunRunning)
	require.NoError(t, err)

	got, err := store.GetRun(ctx, "ntv-1")
	require.NoError(t, err)
	assert.Equal(t, RunRunning, got.Status)
}

func TestUpdateStepState(t *testing.T) {
	ctx := context.Background()
	store := newTestStore(t)

	require.NoError(t, store.CreateRun(ctx, &WorkflowRun{
		ID:         "uss-1",
		WorkflowID: "wf-1",
		Steps:      map[string]StepState{"s1": {StepID: "s1", Status: RunPending}},
	}))

	err := store.UpdateStepState(ctx, "uss-1", StepState{
		StepID: "s1",
		Status: RunCompleted,
		Output: "done",
	})
	require.NoError(t, err)

	got, err := store.GetRun(ctx, "uss-1")
	require.NoError(t, err)
	assert.Equal(t, RunCompleted, got.Steps["s1"].Status)
	assert.Equal(t, "done", got.Steps["s1"].Output)
}

func TestUpdateStepStateAddsNewStep(t *testing.T) {
	ctx := context.Background()
	store := newTestStore(t)

	require.NoError(t, store.CreateRun(ctx, &WorkflowRun{ID: "usn-1", WorkflowID: "wf-1"}))

	// Add a step that wasn't in the initial Steps map.
	err := store.UpdateStepState(ctx, "usn-1", StepState{
		StepID: "new-step",
		Status: RunRunning,
	})
	require.NoError(t, err)

	got, err := store.GetRun(ctx, "usn-1")
	require.NoError(t, err)
	assert.Equal(t, RunRunning, got.Steps["new-step"].Status)
}

func TestUpdateStepStateNotFound(t *testing.T) {
	ctx := context.Background()
	store := newTestStore(t)

	err := store.UpdateStepState(ctx, "nonexistent", StepState{StepID: "s1", Status: RunRunning})
	assert.ErrorIs(t, err, ErrRunNotFound)
}

func TestDeleteRun(t *testing.T) {
	ctx := context.Background()
	store := newTestStore(t)

	require.NoError(t, store.CreateRun(ctx, &WorkflowRun{ID: "del-1", WorkflowID: "wf-1"}))

	err := store.DeleteRun(ctx, "del-1")
	require.NoError(t, err)

	_, err = store.GetRun(ctx, "del-1")
	assert.ErrorIs(t, err, ErrRunNotFound)
}

func TestDeleteRunNotFound(t *testing.T) {
	ctx := context.Background()
	store := newTestStore(t)

	err := store.DeleteRun(ctx, "nonexistent")
	assert.ErrorIs(t, err, ErrRunNotFound)
}

func TestDeleteRunAnyStatus(t *testing.T) {
	ctx := context.Background()
	store := newTestStore(t)

	statuses := []RunStatus{RunPending, RunRunning, RunCompleted, RunFailed, RunCancelled}
	for i, status := range statuses {
		id := "del-status-" + string(rune('a'+i))
		require.NoError(t, store.CreateRun(ctx, &WorkflowRun{ID: id, WorkflowID: "wf-1", Status: status}))

		err := store.DeleteRun(ctx, id)
		require.NoError(t, err, "should delete run with status %d", status)
	}
}

func TestDeleteRunThenUpdateStepReturnsNotFound(t *testing.T) {
	ctx := context.Background()
	store := newTestStore(t)

	require.NoError(t, store.CreateRun(ctx, &WorkflowRun{ID: "dtu-1", WorkflowID: "wf-1"}))
	require.NoError(t, store.DeleteRun(ctx, "dtu-1"))

	err := store.UpdateStepState(ctx, "dtu-1", StepState{StepID: "s1", Status: RunRunning})
	assert.ErrorIs(t, err, ErrRunNotFound)
}

func TestStoreInterface(t *testing.T) {
	// Compile-time check that InMemoryStore implements Store.
	var _ Store = (*InMemoryStore)(nil)
}

func TestConcurrentReadWrite(t *testing.T) {
	ctx := context.Background()
	store := newTestStore(t)

	require.NoError(t, store.CreateRun(ctx, &WorkflowRun{ID: "conc-1", WorkflowID: "wf-1"}))

	var wg sync.WaitGroup
	const goroutines = 50

	// Concurrent writers: update status.
	for i := 0; i < goroutines; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_ = store.UpdateRunStatus(ctx, "conc-1", RunRunning)
		}()
	}

	// Concurrent readers.
	for i := 0; i < goroutines; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_, _ = store.GetRun(ctx, "conc-1")
		}()
	}

	// Concurrent list.
	for i := 0; i < goroutines/5; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_, _ = store.ListRuns(ctx)
		}()
	}

	wg.Wait()
}

func TestConcurrentUpdateStepStateSameRun(t *testing.T) {
	ctx := context.Background()
	store := newTestStore(t)

	require.NoError(t, store.CreateRun(ctx, &WorkflowRun{ID: "css-1", WorkflowID: "wf-1"}))

	var wg sync.WaitGroup
	const steps = 20

	// Simulate parallel stage execution: multiple goroutines updating
	// different step IDs on the same run concurrently.
	for i := 0; i < steps; i++ {
		wg.Add(1)
		stepID := "step-" + string(rune('a'+i))
		go func(sid string) {
			defer wg.Done()
			err := store.UpdateStepState(ctx, "css-1", StepState{
				StepID:    sid,
				Status:    RunCompleted,
				Output:    "output-" + sid,
				StartedAt: time.Now(),
			})
			assert.NoError(t, err)
		}(stepID)
	}

	wg.Wait()

	// Verify all steps were written.
	got, err := store.GetRun(ctx, "css-1")
	require.NoError(t, err)
	assert.Len(t, got.Steps, steps)
}

func TestConcurrentCreateAndDelete(t *testing.T) {
	ctx := context.Background()
	store := newTestStore(t)

	var wg sync.WaitGroup
	const runs = 30

	// Create many runs concurrently.
	for i := 0; i < runs; i++ {
		wg.Add(1)
		id := "ccd-" + string(rune('a'+i))
		go func(runID string) {
			defer wg.Done()
			_ = store.CreateRun(ctx, &WorkflowRun{ID: runID, WorkflowID: "wf-1"})
		}(id)
	}
	wg.Wait()

	// Delete them all concurrently.
	for i := 0; i < runs; i++ {
		wg.Add(1)
		id := "ccd-" + string(rune('a'+i))
		go func(runID string) {
			defer wg.Done()
			_ = store.DeleteRun(ctx, runID)
		}(id)
	}
	wg.Wait()

	remaining, err := store.ListRuns(ctx)
	require.NoError(t, err)
	assert.Empty(t, remaining)
}

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
