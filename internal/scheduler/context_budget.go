package scheduler

import (
	"github.com/mkhomutov/persatrix/internal/planner"
)

// allocateContextBudgets implements RFC 0008 Open Question 8's equal-split
// formulas:
//
//	B_remaining            = B_total - Σ_overrides
//	B_step (non-overridden) = B_remaining / (N_steps - |overrides|)
//
// Per-step overrides (Step.ContextBudget > 0) take precedence; the remainder
// is distributed equally over the non-overridden steps. When all steps carry
// an override, the remainder is dropped (no leftover allocation).
//
// When workflowTotal is 0 the allocator returns nil — packaging is disabled
// and dispatch falls through to legacy behaviour. The planner already rejects
// the case where Σ overrides > workflowTotal at parse time, but this
// function handles it defensively (clamps remaining to 0) so a misconfigured
// store never panics the scheduler.
//
// The returned map is keyed by step ID. Steps with their own override return
// that override verbatim — callers do not need a second lookup.
func allocateContextBudgets(workflowTotal int, steps []planner.Step) map[string]int {
	if workflowTotal <= 0 || len(steps) == 0 {
		return nil
	}

	overrideSum := 0
	overriddenCount := 0
	for _, s := range steps {
		if s.ContextBudget > 0 {
			overrideSum += s.ContextBudget
			overriddenCount++
		}
	}

	remaining := workflowTotal - overrideSum
	if remaining < 0 {
		// Defensive — planner rejects this; preserve safe semantics for any
		// caller that bypasses parse validation (e.g. programmatic Workflow
		// construction in tests).
		remaining = 0
	}

	nonOverridden := len(steps) - overriddenCount
	perStep := 0
	if nonOverridden > 0 {
		perStep = remaining / nonOverridden
	}

	out := make(map[string]int, len(steps))
	for _, s := range steps {
		if s.ContextBudget > 0 {
			out[s.ID] = s.ContextBudget
		} else {
			out[s.ID] = perStep
		}
	}
	return out
}
