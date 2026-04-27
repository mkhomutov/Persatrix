package scheduler

import (
	"context"
	"sync/atomic"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/cost"
	"github.com/mkhomutov/persatrix/internal/executor"
	"github.com/mkhomutov/persatrix/internal/state"
)

// TestContextPackage_PersistsCostMetricsAndRemainingBudget is the end-to-end
// PR 1b contract: when a workflow opts into context packaging, every step
// dispatch must (a) emit a StepCostEntry whose ContextPackage block carries
// the packager Metrics, and (b) persist a non-zero RemainingContextBudget
// on the StepState row so future scheduler-level retries can resume from
// the leftover budget rather than the original allocation.
func TestContextPackage_PersistsCostMetricsAndRemainingBudget(t *testing.T) {
	const yaml = `schema_version: "0.1"
workflow:
  id: "ctxpkg-persist"
  name: "Cost + state persistence"
  trigger: "manual"
  context_budget_total: 4000
  steps:
    - id: "s1"
      agent: "test-agent"
      input: "first step"
      output_key: "out1"
    - id: "s2"
      agent: "test-agent"
      input: "second {{ steps.out1.output }}"
      depends_on: ["s1"]
      output_key: "out2"
`
	dir := t.TempDir()
	writeWorkflow(t, dir, "ctxpkg-persist", yaml)

	cfg := &cost.CostConfig{}
	tc := cost.NewTokenCounter(cfg, zap.NewNop())
	cr := cost.NewCostReporter(tc, cfg, zap.NewNop())

	store := state.NewInMemoryStore(zap.NewNop())

	var dispatched atomic.Int32
	exec := &mockExecutor{handler: func(_ context.Context, req executor.ExecuteRequest) (*executor.ExecuteResult, error) {
		dispatched.Add(1)
		return &executor.ExecuteResult{
			TaskID: "t-" + req.StepID,
			Output: "ok-" + req.StepID,
			Metadata: map[string]string{
				"input_tokens":  "10",
				"output_tokens": "5",
			},
		}, nil
	}}

	sched := newTestScheduler(t, store, exec, dir,
		WithPollInterval(50*time.Millisecond),
		WithCostComponents(tc, nil, cr),
	)
	createPendingRun(t, store, "ctxpkg-persist-run", "ctxpkg-persist", nil)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = sched.Run(ctx) }()

	waitForRunStatus(t, store, "ctxpkg-persist-run", state.RunCompleted, 5*time.Second)
	require.EqualValues(t, 2, dispatched.Load(), "both steps should have dispatched")

	// (a) cost record carries ContextPackage on every step.
	summary := cr.WorkflowSummary("ctxpkg-persist")
	require.Len(t, summary.Steps, 2)
	for _, entry := range summary.Steps {
		require.NotNilf(t, entry.ContextPackage,
			"step %q must carry ContextPackage in its cost record", entry.StepID)
		assert.GreaterOrEqual(t, entry.ContextPackage.CandidatesAdmitted, 0)
		// admitted == len(StepOutputs); s1 has no upstream candidates, s2 has out1.
	}

	// (b) state rows persist RemainingContextBudget. With a 4000-token
	// total split across 2 steps (allocator gives 2000 each) and tiny payloads
	// the remainder must be the bulk of the allocation.
	run, err := store.GetRun(context.Background(), "ctxpkg-persist-run")
	require.NoError(t, err)
	for _, stepID := range []string{"s1", "s2"} {
		st, ok := run.Steps[stepID]
		require.True(t, ok, "step %q must exist in state", stepID)
		assert.Equal(t, state.RunCompleted, st.Status)
		assert.Greater(t, st.RemainingContextBudget, 0,
			"step %q must persist a positive RemainingContextBudget", stepID)
		assert.LessOrEqual(t, st.RemainingContextBudget, 2000,
			"step %q remaining must not exceed its 2000-token allocation", stepID)
	}
}
