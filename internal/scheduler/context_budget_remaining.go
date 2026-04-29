package scheduler

import (
	"context"
	"errors"

	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/cost"
	"github.com/mkhomutov/persatrix/internal/executor/packaging"
	"github.com/mkhomutov/persatrix/internal/state"
)

// remainingContextBudgetForStep resolves the budget the packager should use
// for a step's current attempt. Default is the original allocation produced
// by allocateContextBudgets; if a prior step state already exists in the
// store with a positive RemainingContextBudget (i.e. the step is being
// retried after a successful first attempt persisted leftover budget), the
// remainder is used instead. RFC 0008 PR 1b.
//
// Scheduler-level retry doesn't exist in v0.3 — today every step is a fresh
// dispatch and no prior StepState carries RemainingContextBudget. The lookup
// is wired in PR 1b so that when scheduler-level retry lands (RFC 0008 PR 6
// or later), no second edit is needed in stage_runner: the persisted
// remainder is honored by construction.
//
// Lookup failures (run not found, store error, missing step) all degrade to
// the original allocation — the budget read is best-effort, never fatal.
//
// L15 (RFC 0008 PR 6a): the allocator never emits a negative `allocated`,
// but the function's contract is "return the budget to use" — clamp to 0
// on the early-return paths so a hypothetical negative input cannot leak
// into the packager.
func (s *WorkflowScheduler) remainingContextBudgetForStep(ctx context.Context, runID, stepID string, allocated int) int {
	if allocated < 0 {
		return 0
	}
	if s.store == nil || allocated == 0 {
		return allocated
	}
	run, err := s.store.GetRun(ctx, runID)
	if err != nil {
		if !errors.Is(err, state.ErrRunNotFound) {
			s.logger.Debug("remaining context budget lookup failed; using full allocation",
				zap.String("execution_id", runID),
				zap.String("step_id", stepID),
				zap.Error(err),
			)
		}
		return allocated
	}
	prior, ok := run.Steps[stepID]
	if !ok {
		return allocated
	}
	if prior.RemainingContextBudget <= 0 {
		return allocated
	}
	// Defensive cap: never hand the packager more than the original
	// allocation, even if a buggy writer somehow persisted a larger remainder.
	if prior.RemainingContextBudget > allocated {
		return allocated
	}
	return prior.RemainingContextBudget
}

// remainingFromPackage computes the budget left over after packaging admits
// pkg.Metrics.TokensAfter tokens against `effectiveBudget`. Returns zero
// (the StepState default) when the package is nil — the workflow didn't opt
// into context packaging on this step. Negative values are clamped to zero
// so the pinned-overflow path (the only known site that can produce
// TokensAfter > effectiveBudget today) cannot persist a negative remainder.
func remainingFromPackage(effectiveBudget int, pkg *packaging.Package) int {
	if pkg == nil || effectiveBudget <= 0 {
		return 0
	}
	remaining := effectiveBudget - pkg.Metrics.TokensAfter
	if remaining < 0 {
		return 0
	}
	return remaining
}

// contextPackageMetricsFromPackage builds the cost-record-side metrics block
// from a packaging.Package. Returns nil when pkg is nil so the StepCostEntry
// keeps its pre-PR-1b shape under `omitempty` for steps in workflows that
// don't opt into context packaging.
func contextPackageMetricsFromPackage(pkg *packaging.Package) *cost.ContextPackageMetrics {
	if pkg == nil {
		return nil
	}
	return cost.NewContextPackageMetrics(pkg)
}
