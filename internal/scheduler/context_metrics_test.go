package scheduler

import (
	"context"
	"strings"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.uber.org/zap"
	"go.uber.org/zap/zaptest/observer"

	"github.com/mkhomutov/persatrix/internal/cost"
	"github.com/mkhomutov/persatrix/internal/executor"
	"github.com/mkhomutov/persatrix/internal/executor/packaging"
	"github.com/mkhomutov/persatrix/internal/planner"
	"github.com/mkhomutov/persatrix/internal/state"
)

// TestAttachContextPackage_WarningSamplerDedupesPerStep asserts the L11
// review follow-up: the warn channel emits each (execution_id, step_id,
// warning) tuple at most once per run, even when the same step re-attaches
// its package N times (the future scheduler-level retry path).
// The cost record on StepCostEntry.ContextPackage carries the unsampled
// metric for every attempt, so capping log noise here loses no telemetry.
func TestAttachContextPackage_WarningSamplerDedupesPerStep(t *testing.T) {
	core, recorded := observer.New(zap.WarnLevel)
	s := &WorkflowScheduler{
		logger:   zap.New(core),
		packager: packaging.NewPackager(nil),
	}
	step := planner.Step{ID: "s1"}

	// Three back-to-back attaches with the same (run, step) — the packager
	// will fire the same warning every time because the inputs are unchanged.
	for range 3 {
		outputs := map[string]string{
			"big": strings.Repeat("x", 4000),
		}
		_, err := s.attachContextPackage(outputs, "run-sampler-1", step, "input", 10)
		require.NoError(t, err)
	}

	entries := recorded.FilterMessage("context_package warning").All()
	require.Len(t, entries, 1,
		"sampler should cap repeated warnings per (run, step, kind) tuple at one emission")
}

// TestAttachContextPackage_WarningSamplerKeysOnRunAndStep asserts the
// sampler scopes by execution_id and step_id — a different run or a
// different step always gets a fresh emission budget so cross-run
// regressions stay visible.
func TestAttachContextPackage_WarningSamplerKeysOnRunAndStep(t *testing.T) {
	core, recorded := observer.New(zap.WarnLevel)
	s := &WorkflowScheduler{
		logger:   zap.New(core),
		packager: packaging.NewPackager(nil),
	}

	// Same step, different runs — should produce one warning per run.
	for _, runID := range []string{"run-A", "run-B"} {
		outputs := map[string]string{"big": strings.Repeat("x", 4000)}
		_, err := s.attachContextPackage(outputs, runID, planner.Step{ID: "s1"}, "input", 10)
		require.NoError(t, err)
	}
	// Same run, different step — also a fresh tuple.
	outputs := map[string]string{"big": strings.Repeat("x", 4000)}
	_, err := s.attachContextPackage(outputs, "run-A", planner.Step{ID: "s2"}, "input", 10)
	require.NoError(t, err)

	entries := recorded.FilterMessage("context_package warning").All()
	require.Len(t, entries, 3, "each unique (run, step) tuple should emit one warning")
}

// TestAttachContextPackage_ReturnsPackage asserts the PR 1b plumbing: the
// caller receives the built *packaging.Package so it can route Metrics into
// the cost record and compute RemainingContextBudget.
func TestAttachContextPackage_ReturnsPackage(t *testing.T) {
	s := &WorkflowScheduler{
		logger:   zap.NewNop(),
		packager: packaging.NewPackager(nil),
	}
	outputs := map[string]string{"a": "hello", "b": "world"}
	pkg, err := s.attachContextPackage(outputs, "run-1", planner.Step{ID: "s1"}, "input", 1000)
	require.NoError(t, err)
	require.NotNil(t, pkg)
	assert.Equal(t, 1, pkg.Version)
	assert.Greater(t, len(pkg.StepOutputs), 0)
}

// TestRecordStepUsage_AttachesContextPackageMetrics asserts that when the
// scheduler records cost for a step that built a context package, the
// StepCostEntry carries a ContextPackageMetrics block derived from
// pkg.Metrics + len(pkg.StepOutputs). This is the new wiring PR 1b adds on
// top of the PR 1 packaging foundation.
func TestRecordStepUsage_AttachesContextPackageMetrics(t *testing.T) {
	cfg := &cost.CostConfig{}
	counter := cost.NewTokenCounter(cfg, zap.NewNop())
	reporter := cost.NewCostReporter(counter, cfg, zap.NewNop())
	s := &WorkflowScheduler{
		logger:       zap.NewNop(),
		tokenCounter: counter,
		costReporter: reporter,
	}

	pkg := &packaging.Package{
		Version: 1,
		StepOutputs: []packaging.AdmittedSection{
			{ID: "a", Content: "alpha"},
			{ID: "b", Content: "beta"},
		},
		Metrics: packaging.Metrics{
			TokensBefore:      120,
			TokensAfter:       80,
			CompressionRatio:  1.5,
			CandidatesDropped: 1,
		},
	}
	step := planner.Step{ID: "s1", AgentID: "agent-1"}
	result := &executor.ExecuteResult{
		Output:   "ok",
		Metadata: map[string]string{"input_tokens": "10", "output_tokens": "20"},
	}

	s.recordStepUsage("wf-1", step, result, "claude-3", pkg)

	summary := reporter.WorkflowSummary("wf-1")
	require.Len(t, summary.Steps, 1)
	require.NotNil(t, summary.Steps[0].ContextPackage,
		"context package metrics should be attached to the cost record when pkg != nil")
	cpm := summary.Steps[0].ContextPackage
	assert.Equal(t, 120, cpm.TokensBefore)
	assert.Equal(t, 80, cpm.TokensAfter)
	assert.Equal(t, 1.5, cpm.CompressionRatio)
	assert.Equal(t, 2, cpm.CandidatesAdmitted, "admitted == len(pkg.StepOutputs)")
	assert.Equal(t, 1, cpm.CandidatesDropped)
}

// TestRecordStepUsage_NilPackageOmitsContextPackageMetrics confirms the
// pre-PR-1b shape is preserved for steps in workflows that don't opt into
// context packaging — `omitempty` drops the ContextPackage field entirely.
func TestRecordStepUsage_NilPackageOmitsContextPackageMetrics(t *testing.T) {
	cfg := &cost.CostConfig{}
	counter := cost.NewTokenCounter(cfg, zap.NewNop())
	reporter := cost.NewCostReporter(counter, cfg, zap.NewNop())
	s := &WorkflowScheduler{
		logger:       zap.NewNop(),
		tokenCounter: counter,
		costReporter: reporter,
	}

	step := planner.Step{ID: "s1", AgentID: "agent-1"}
	result := &executor.ExecuteResult{
		Output:   "ok",
		Metadata: map[string]string{"input_tokens": "10", "output_tokens": "20"},
	}
	s.recordStepUsage("wf-1", step, result, "claude-3", nil)

	summary := reporter.WorkflowSummary("wf-1")
	require.Len(t, summary.Steps, 1)
	assert.Nil(t, summary.Steps[0].ContextPackage,
		"nil pkg → nil ContextPackage in cost record (pre-PR-1b shape preserved)")
}

// TestRemainingFromPackage covers the small clamp helper that computes
// RemainingContextBudget = effective - TokensAfter, clamped to zero.
// Negative remainders happen on the pinned-overflow path (the only known
// site that can admit more than the budget today).
func TestRemainingFromPackage(t *testing.T) {
	t.Run("nil package returns zero", func(t *testing.T) {
		assert.Equal(t, 0, remainingFromPackage(1000, nil))
	})
	t.Run("zero budget returns zero", func(t *testing.T) {
		pkg := &packaging.Package{Metrics: packaging.Metrics{TokensAfter: 100}}
		assert.Equal(t, 0, remainingFromPackage(0, pkg))
	})
	t.Run("normal subtraction", func(t *testing.T) {
		pkg := &packaging.Package{Metrics: packaging.Metrics{TokensAfter: 300}}
		assert.Equal(t, 700, remainingFromPackage(1000, pkg))
	})
	t.Run("negative remainder clamps to zero", func(t *testing.T) {
		pkg := &packaging.Package{Metrics: packaging.Metrics{TokensAfter: 1500}}
		assert.Equal(t, 0, remainingFromPackage(1000, pkg))
	})
}

// TestRemainingContextBudgetForStep_NoPriorState asserts the lookup returns
// the original allocation when the step has never run before — the common
// case on first dispatch.
func TestRemainingContextBudgetForStep_NoPriorState(t *testing.T) {
	store := state.NewInMemoryStore(zap.NewNop())
	require.NoError(t, store.CreateRun(context.Background(), &state.WorkflowRun{ID: "run-1", WorkflowID: "wf-1"}))
	s := &WorkflowScheduler{store: store, logger: zap.NewNop()}

	got := s.remainingContextBudgetForStep(context.Background(), "run-1", "s1", 1000)
	assert.Equal(t, 1000, got)
}

// TestRemainingContextBudgetForStep_UsesPersistedRemainder asserts the
// retry-path contract: when prior step state carries a positive
// RemainingContextBudget, that value is preferred over the original
// allocation. This is the "consume from the persisted remainder" semantic
// the PR plan calls out for scheduler-level retries.
func TestRemainingContextBudgetForStep_UsesPersistedRemainder(t *testing.T) {
	store := state.NewInMemoryStore(zap.NewNop())
	ctx := context.Background()
	require.NoError(t, store.CreateRun(ctx, &state.WorkflowRun{ID: "run-1", WorkflowID: "wf-1"}))
	require.NoError(t, store.UpdateStepState(ctx, "run-1", state.StepState{
		StepID:                 "s1",
		Status:                 state.RunRetrying,
		RemainingContextBudget: 250,
	}))
	s := &WorkflowScheduler{store: store, logger: zap.NewNop()}

	got := s.remainingContextBudgetForStep(ctx, "run-1", "s1", 1000)
	assert.Equal(t, 250, got, "should use the persisted remainder on retry")
}

// TestRemainingContextBudgetForStep_RemainderCappedAtAllocation defends
// against a buggy writer persisting a remainder larger than the original
// allocation — the lookup must never hand the packager more tokens than the
// allocator originally granted.
//
// L16 (RFC 0008 PR 6a): set Status explicitly to RunCompleted so the test
// would still exercise the intended path if the function ever filters by
// status (currently it does not, but the explicit status documents intent
// and prevents a future status-gated read from silently passing).
func TestRemainingContextBudgetForStep_RemainderCappedAtAllocation(t *testing.T) {
	store := state.NewInMemoryStore(zap.NewNop())
	ctx := context.Background()
	require.NoError(t, store.CreateRun(ctx, &state.WorkflowRun{ID: "run-1", WorkflowID: "wf-1"}))
	require.NoError(t, store.UpdateStepState(ctx, "run-1", state.StepState{
		StepID:                 "s1",
		Status:                 state.RunCompleted,
		RemainingContextBudget: 5000,
	}))
	s := &WorkflowScheduler{store: store, logger: zap.NewNop()}

	got := s.remainingContextBudgetForStep(ctx, "run-1", "s1", 1000)
	assert.Equal(t, 1000, got, "remainder must never exceed original allocation")
}

// TestRemainingContextBudgetForStep_NilStoreOrZeroAllocationDegradesGracefully
// covers the defensive early-return branches.
func TestRemainingContextBudgetForStep_NilStoreOrZeroAllocationDegradesGracefully(t *testing.T) {
	t.Run("nil store", func(t *testing.T) {
		s := &WorkflowScheduler{logger: zap.NewNop()}
		assert.Equal(t, 1000, s.remainingContextBudgetForStep(context.Background(), "r", "s", 1000))
	})
	t.Run("zero allocation", func(t *testing.T) {
		store := state.NewInMemoryStore(zap.NewNop())
		s := &WorkflowScheduler{store: store, logger: zap.NewNop()}
		assert.Equal(t, 0, s.remainingContextBudgetForStep(context.Background(), "r", "s", 0))
	})
	t.Run("run not found", func(t *testing.T) {
		store := state.NewInMemoryStore(zap.NewNop())
		s := &WorkflowScheduler{store: store, logger: zap.NewNop()}
		assert.Equal(t, 1000, s.remainingContextBudgetForStep(context.Background(), "missing", "s", 1000))
	})
}
