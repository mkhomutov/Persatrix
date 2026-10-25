package scheduler

import (
	"context"
	"fmt"
	"sync"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/executor"
	"github.com/mkhomutov/persatrix/internal/state"
)

// sealCall records one SealAfterGrace call and the run's stored status at
// that moment, so a test can check the seal comes after the final status.
type sealCall struct {
	executionID string
	status      state.RunStatus
}

// fakeLogSealer stands in for *logbuffer.Buffer.
type fakeLogSealer struct {
	store state.Store

	mu    sync.Mutex
	calls []sealCall
}

func (f *fakeLogSealer) SealAfterGrace(executionID string) {
	status := state.RunStatus(-1)
	if run, err := f.store.GetRun(context.Background(), executionID); err == nil {
		status = run.Status
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls = append(f.calls, sealCall{executionID: executionID, status: status})
}

func (f *fakeLogSealer) snapshot() []sealCall {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]sealCall(nil), f.calls...)
}

// A run's log ring must be sealed once the run reaches a terminal state,
// whichever way it ends, so its entries are written to disk and survive an
// orchestrator restart (RFC 0018 § E). Before this hook nothing in
// production called Seal, so no execution's logs ever reached disk.
func TestTerminalRun_SealsRunLogs(t *testing.T) {
	tests := []struct {
		name     string
		yaml     string
		stepErr  error
		expected state.RunStatus
	}{
		{name: "completed", yaml: singleStepYAML, expected: state.RunCompleted},
		{name: "step fails", yaml: singleStepYAML, stepErr: fmt.Errorf("step failed intentionally"), expected: state.RunFailed},
		{name: "workflow does not parse", yaml: badYAML, expected: state.RunFailed},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			dir := t.TempDir()
			writeWorkflow(t, dir, "test-wf", tt.yaml)
			store := state.NewInMemoryStore(zap.NewNop())
			sealer := &fakeLogSealer{store: store}

			exec := &mockExecutor{handler: func(context.Context, executor.ExecuteRequest) (*executor.ExecuteResult, error) {
				if tt.stepErr != nil {
					return nil, tt.stepErr
				}
				return &executor.ExecuteResult{TaskID: "t1", Output: "done"}, nil
			}}
			sched := newTestScheduler(t, store, exec, dir,
				WithPollInterval(20*time.Millisecond), WithLogSealer(sealer))
			createPendingRun(t, store, "seal-run", "test-wf", nil)

			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			go func() { _ = sched.Run(ctx) }()

			waitForRunStatus(t, store, "seal-run", tt.expected, 5*time.Second)
			require.Eventually(t, func() bool { return len(sealer.snapshot()) > 0 },
				2*time.Second, 10*time.Millisecond, "run reached a terminal state but its logs were never sealed")

			// Give a stray second call time to show up before checking the count.
			time.Sleep(100 * time.Millisecond)
			calls := sealer.snapshot()
			require.Len(t, calls, 1, "a run's logs must be sealed exactly once")
			assert.Equal(t, "seal-run", calls[0].executionID)
			assert.Equal(t, tt.expected, calls[0].status,
				"the seal must come after the terminal status is stored")
		})
	}
}

// Without a sealer the scheduler behaves exactly as before.
func TestTerminalRun_NoSealerIsSafe(t *testing.T) {
	dir := t.TempDir()
	writeWorkflow(t, dir, "test-wf", singleStepYAML)
	store := state.NewInMemoryStore(zap.NewNop())
	exec := &mockExecutor{handler: func(context.Context, executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		return &executor.ExecuteResult{TaskID: "t1", Output: "done"}, nil
	}}
	sched := newTestScheduler(t, store, exec, dir, WithPollInterval(20*time.Millisecond))
	createPendingRun(t, store, "no-sealer", "test-wf", nil)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = sched.Run(ctx) }()

	waitForRunStatus(t, store, "no-sealer", state.RunCompleted, 5*time.Second)
}
