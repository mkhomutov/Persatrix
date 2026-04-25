package state

import (
	"context"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

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

// --- StepExecutionMetadata tests (RFC 0006 PR 4a) ---

func TestUpdateStepState_WithMetadata(t *testing.T) {
	ctx := context.Background()
	store := newTestStore(t)

	require.NoError(t, store.CreateRun(ctx, &WorkflowRun{
		ID: "meta-1", WorkflowID: "wf-1",
	}))

	meta := &StepExecutionMetadata{
		TokensUsed:       1500,
		LLMCallCount:     3,
		RetryCount:       1,
		CacheHit:         false,
		WallTimeMs:       2500,
		EstimatedCostUSD: 0.0045,
	}
	err := store.UpdateStepState(ctx, "meta-1", StepState{
		StepID:   "step-1",
		Status:   RunCompleted,
		Output:   "done",
		Metadata: meta,
	})
	require.NoError(t, err)

	got, err := store.GetRun(ctx, "meta-1")
	require.NoError(t, err)
	require.NotNil(t, got.Steps["step-1"].Metadata)
	assert.Equal(t, 1500, got.Steps["step-1"].Metadata.TokensUsed)
	assert.Equal(t, 3, got.Steps["step-1"].Metadata.LLMCallCount)
	assert.Equal(t, 1, got.Steps["step-1"].Metadata.RetryCount)
	assert.False(t, got.Steps["step-1"].Metadata.CacheHit)
	assert.Equal(t, int64(2500), got.Steps["step-1"].Metadata.WallTimeMs)
	assert.InDelta(t, 0.0045, got.Steps["step-1"].Metadata.EstimatedCostUSD, 1e-9)
}

func TestUpdateStepState_NilMetadata(t *testing.T) {
	ctx := context.Background()
	store := newTestStore(t)

	require.NoError(t, store.CreateRun(ctx, &WorkflowRun{
		ID: "meta-nil", WorkflowID: "wf-1",
	}))

	err := store.UpdateStepState(ctx, "meta-nil", StepState{
		StepID: "step-1",
		Status: RunCompleted,
	})
	require.NoError(t, err)

	got, err := store.GetRun(ctx, "meta-nil")
	require.NoError(t, err)
	assert.Nil(t, got.Steps["step-1"].Metadata)
}

func TestGetRunDeepCopy_MetadataIsolation(t *testing.T) {
	ctx := context.Background()
	store := newTestStore(t)

	require.NoError(t, store.CreateRun(ctx, &WorkflowRun{
		ID: "meta-iso", WorkflowID: "wf-1",
	}))

	meta := &StepExecutionMetadata{
		TokensUsed: 100,
		WallTimeMs: 500,
	}
	require.NoError(t, store.UpdateStepState(ctx, "meta-iso", StepState{
		StepID:   "step-1",
		Status:   RunCompleted,
		Metadata: meta,
	}))

	// Get a copy and mutate its metadata.
	got, err := store.GetRun(ctx, "meta-iso")
	require.NoError(t, err)
	got.Steps["step-1"].Metadata.TokensUsed = 9999

	// Original in store should be unchanged.
	got2, err := store.GetRun(ctx, "meta-iso")
	require.NoError(t, err)
	assert.Equal(t, 100, got2.Steps["step-1"].Metadata.TokensUsed)
}

// TestUpdateStepState_WriteIsolation verifies that mutating a metadata pointer
// AFTER calling UpdateStepState does not corrupt the store's internal state.
// This complements TestGetRunDeepCopy_MetadataIsolation (read isolation) by
// testing the write path. (PR 5a, M-02 fix)
func TestUpdateStepState_WriteIsolation(t *testing.T) {
	ctx := context.Background()
	store := newTestStore(t)

	require.NoError(t, store.CreateRun(ctx, &WorkflowRun{
		ID: "write-iso", WorkflowID: "wf-1",
	}))

	meta := &StepExecutionMetadata{
		TokensUsed:       200,
		WallTimeMs:       1000,
		EstimatedCostUSD: 0.05,
	}
	require.NoError(t, store.UpdateStepState(ctx, "write-iso", StepState{
		StepID:   "step-1",
		Status:   RunCompleted,
		Metadata: meta,
	}))

	// Mutate the original metadata pointer AFTER UpdateStepState.
	meta.TokensUsed = 9999
	meta.WallTimeMs = 0
	meta.EstimatedCostUSD = 999.99

	// Store's internal copy should be unaffected.
	got, err := store.GetRun(ctx, "write-iso")
	require.NoError(t, err)
	require.NotNil(t, got.Steps["step-1"].Metadata)
	assert.Equal(t, 200, got.Steps["step-1"].Metadata.TokensUsed,
		"store metadata should not be affected by caller mutation")
	assert.Equal(t, int64(1000), got.Steps["step-1"].Metadata.WallTimeMs,
		"store metadata should not be affected by caller mutation")
	assert.InDelta(t, 0.05, got.Steps["step-1"].Metadata.EstimatedCostUSD, 0.001,
		"store metadata should not be affected by caller mutation")
}
