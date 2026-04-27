package planner

import "fmt"

// validateContextBudget enforces RFC 0008 Phase 1 invariants on the
// workflow-level `context_budget_total` and per-step `context_budget` fields.
//
// Extracted from validate() during RFC 0008 PR 1b (review finding L12) so the
// next planner edit doesn't have to navigate around two unrelated validation
// concerns in the same function. The contract is unchanged from PR 1.
//
// The rules are:
//
//  1. `context_budget_total` must not be negative.
//  2. When `context_budget_total > 0`, the sum of per-step `context_budget`
//     overrides must not exceed it (the equal-split allocator would otherwise
//     produce a negative remainder for non-overridden steps).
//  3. When `context_budget_total` is unset (== 0), per-step `context_budget`
//     must also be unset — silently dropping the per-step override would be a
//     deny-by-default footgun (the author opts in to packaging on a step but
//     gets legacy passthrough). Caller must either set the workflow total or
//     remove the per-step override.
//
// Per-step negative-`context_budget` rejection lives in the per-step loop in
// validate() alongside the other RFC 0006 limit-field checks; keeping it
// there preserves a single error-message shape for the per-step block.
func validateContextBudget(w *Workflow) error {
	if w.ContextBudgetTotal < 0 {
		return fmt.Errorf("workflow %q: context_budget_total must not be negative", w.ID)
	}
	if w.ContextBudgetTotal > 0 {
		var overrideSum int
		for _, step := range w.Steps {
			if step.ContextBudget > 0 {
				overrideSum += step.ContextBudget
			}
		}
		if overrideSum > w.ContextBudgetTotal {
			return fmt.Errorf("workflow %q: sum of per-step context_budget overrides (%d) exceeds context_budget_total (%d)",
				w.ID, overrideSum, w.ContextBudgetTotal)
		}
		return nil
	}
	// context_budget_total unset (== 0): reject any per-step context_budget
	// (RFC 0008 PR 1, finding H3).
	for _, step := range w.Steps {
		if step.ContextBudget > 0 {
			return fmt.Errorf("step %q: context_budget set (%d) but workflow.context_budget_total is unset; either set the workflow total or remove the per-step override",
				step.ID, step.ContextBudget)
		}
	}
	return nil
}
