package scheduler

import (
	"testing"

	"github.com/stretchr/testify/assert"

	"github.com/mkhomutov/persatrix/internal/planner"
)

// --- RFC 0008 PR 1: equal-split context-budget allocator ---

func TestAllocateContextBudgets_NoTotal_ReturnsNil(t *testing.T) {
	steps := []planner.Step{{ID: "s1"}, {ID: "s2"}}
	got := allocateContextBudgets(0, steps)
	assert.Nil(t, got, "zero total disables packaging — must return nil")
}

func TestAllocateContextBudgets_EmptySteps_ReturnsNil(t *testing.T) {
	got := allocateContextBudgets(1000, nil)
	assert.Nil(t, got)
}

func TestAllocateContextBudgets_EqualSplit_NoOverrides(t *testing.T) {
	steps := []planner.Step{{ID: "s1"}, {ID: "s2"}, {ID: "s3"}}
	got := allocateContextBudgets(6000, steps)
	assert.Equal(t, map[string]int{"s1": 2000, "s2": 2000, "s3": 2000}, got)
}

func TestAllocateContextBudgets_MixedOverrides(t *testing.T) {
	steps := []planner.Step{
		{ID: "s1", ContextBudget: 4000},
		{ID: "s2"},
		{ID: "s3"},
	}
	got := allocateContextBudgets(6000, steps)
	// s1 keeps its override; remainder 2000 splits across s2+s3 → 1000 each.
	assert.Equal(t, map[string]int{"s1": 4000, "s2": 1000, "s3": 1000}, got)
}

func TestAllocateContextBudgets_AllOverridden(t *testing.T) {
	steps := []planner.Step{
		{ID: "s1", ContextBudget: 600},
		{ID: "s2", ContextBudget: 400},
	}
	got := allocateContextBudgets(1000, steps)
	assert.Equal(t, map[string]int{"s1": 600, "s2": 400}, got)
}

func TestAllocateContextBudgets_OverrideSumExceedsTotal_ClampsRemainder(t *testing.T) {
	// Planner rejects this at parse, but the allocator must not panic if a
	// caller bypasses parse validation.
	steps := []planner.Step{
		{ID: "s1", ContextBudget: 800},
		{ID: "s2", ContextBudget: 800},
		{ID: "s3"},
	}
	got := allocateContextBudgets(1000, steps)
	// Overrides honoured; non-overridden step gets 0 (clamped).
	assert.Equal(t, map[string]int{"s1": 800, "s2": 800, "s3": 0}, got)
}

func TestAllocateContextBudgets_SingleStep(t *testing.T) {
	steps := []planner.Step{{ID: "s1"}}
	got := allocateContextBudgets(1000, steps)
	assert.Equal(t, map[string]int{"s1": 1000}, got)
}

func TestAllocateContextBudgets_TenSteps_DivisionRemainderDropped(t *testing.T) {
	// 1003 / 10 = 100 (integer division — 3 leftover tokens are not redistributed).
	// This is acceptable per Open Question 8: equal-split, no rounding tricks.
	steps := make([]planner.Step, 10)
	for i := range steps {
		steps[i] = planner.Step{ID: string(rune('a' + i))}
	}
	got := allocateContextBudgets(1003, steps)
	for _, v := range got {
		assert.Equal(t, 100, v)
	}
}
