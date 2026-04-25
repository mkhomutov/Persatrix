package state

import (
	"context"
	"fmt"
	"sync"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

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
	// different step IDs on the same run concurrently. Half the steps
	// include Metadata pointers to exercise the M-02 deep-copy under -race.
	for i := 0; i < steps; i++ {
		wg.Add(1)
		stepID := fmt.Sprintf("step-%d", i)
		go func(sid string, idx int) {
			defer wg.Done()
			step := StepState{
				StepID:    sid,
				Status:    RunCompleted,
				Output:    "output-" + sid,
				StartedAt: time.Now(),
			}
			// Even-numbered steps carry metadata pointers.
			if idx%2 == 0 {
				step.Metadata = &StepExecutionMetadata{
					TokensUsed:       100 * (idx + 1),
					WallTimeMs:       int64(50 * (idx + 1)),
					EstimatedCostUSD: 0.01 * float64(idx+1),
				}
			}
			err := store.UpdateStepState(ctx, "css-1", step)
			assert.NoError(t, err)
		}(stepID, i)
	}

	wg.Wait()

	// Verify all steps were written.
	got, err := store.GetRun(ctx, "css-1")
	require.NoError(t, err)
	assert.Len(t, got.Steps, steps)

	// Verify metadata on even-numbered steps survived the concurrent writes.
	for i := 0; i < steps; i++ {
		sid := fmt.Sprintf("step-%d", i)
		step := got.Steps[sid]
		if i%2 == 0 {
			require.NotNil(t, step.Metadata, "step %s should have metadata", sid)
			assert.Equal(t, 100*(i+1), step.Metadata.TokensUsed)
		} else {
			assert.Nil(t, step.Metadata, "step %s should have no metadata", sid)
		}
	}
}

func TestConcurrentCreateAndDelete(t *testing.T) {
	ctx := context.Background()
	store := newTestStore(t)

	var wg sync.WaitGroup
	const runs = 30

	// Create many runs concurrently.
	for i := 0; i < runs; i++ {
		wg.Add(1)
		id := fmt.Sprintf("ccd-%d", i)
		go func(runID string) {
			defer wg.Done()
			_ = store.CreateRun(ctx, &WorkflowRun{ID: runID, WorkflowID: "wf-1"})
		}(id)
	}
	wg.Wait()

	// Delete them all concurrently.
	for i := 0; i < runs; i++ {
		wg.Add(1)
		id := fmt.Sprintf("ccd-%d", i)
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

func TestConcurrentTimestampsAndErrors(t *testing.T) {
	// N-20: Exercise SetRunTimestamps and SetRunError under -race with concurrent goroutines.
	ctx := context.Background()
	store := newTestStore(t)

	const runs = 20
	for i := 0; i < runs; i++ {
		id := fmt.Sprintf("cte-%d", i)
		require.NoError(t, store.CreateRun(ctx, &WorkflowRun{ID: id, WorkflowID: "wf-1"}))
	}

	var wg sync.WaitGroup

	// Concurrent SetRunTimestamps writers.
	for i := 0; i < runs; i++ {
		wg.Add(1)
		id := fmt.Sprintf("cte-%d", i)
		go func(runID string) {
			defer wg.Done()
			now := time.Now()
			_ = store.SetRunTimestamps(ctx, runID, &now, nil)
		}(id)
	}

	// Concurrent SetRunError writers.
	for i := 0; i < runs; i++ {
		wg.Add(1)
		id := fmt.Sprintf("cte-%d", i)
		go func(runID string) {
			defer wg.Done()
			_ = store.SetRunError(ctx, runID, "concurrent error for "+runID)
		}(id)
	}

	// Concurrent readers interleaved with writers.
	for i := 0; i < runs; i++ {
		wg.Add(1)
		id := fmt.Sprintf("cte-%d", i)
		go func(runID string) {
			defer wg.Done()
			_, _ = store.GetRun(ctx, runID)
		}(id)
	}

	wg.Wait()

	// Verify all runs have timestamps and errors set.
	for i := 0; i < runs; i++ {
		id := fmt.Sprintf("cte-%d", i)
		got, err := store.GetRun(ctx, id)
		require.NoError(t, err)
		assert.False(t, got.StartedAt.IsZero(), "run %s should have StartedAt set", id)
		assert.Equal(t, "concurrent error for "+id, got.Error, "run %s should have error set", id)
	}
}
