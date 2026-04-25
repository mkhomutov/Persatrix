package state

import (
	"context"
	"fmt"
	"testing"

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

	statuses := []RunStatus{RunPending, RunRunning, RunCompleted, RunFailed, RunCancelled, RunRetrying}
	for i, status := range statuses {
		id := fmt.Sprintf("del-status-%d", i)
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
