package planner

import (
	"context"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// --- Workflow-level context_budget_total tests (RFC 0008 PR 1) ---

func TestParse_WorkflowContextBudgetTotal_Present(t *testing.T) {
	yaml := `
schema_version: "0.1"
workflow:
  id: "test-wf"
  name: "Budget test"
  context_budget_total: 6000
  steps:
    - id: "s1"
      agent: "planner"
      input: "hello"
`
	p := newTestPlanner()
	wf, err := p.Parse(context.Background(), writeTempYAML(t, yaml))
	require.NoError(t, err)
	assert.Equal(t, 6000, wf.ContextBudgetTotal)
}

func TestParse_WorkflowContextBudgetTotal_Absent(t *testing.T) {
	yaml := `
schema_version: "0.1"
workflow:
  id: "test-wf"
  name: "No budget"
  steps:
    - id: "s1"
      agent: "planner"
      input: "hello"
`
	p := newTestPlanner()
	wf, err := p.Parse(context.Background(), writeTempYAML(t, yaml))
	require.NoError(t, err)
	// Absence yields zero (packaging disabled — legacy passthrough).
	assert.Equal(t, 0, wf.ContextBudgetTotal)
}

func TestParse_WorkflowContextBudgetTotal_Negative(t *testing.T) {
	yaml := `
schema_version: "0.1"
workflow:
  id: "test-wf"
  name: "Neg budget"
  context_budget_total: -1
  steps:
    - id: "s1"
      agent: "planner"
      input: "hello"
`
	p := newTestPlanner()
	_, err := p.Parse(context.Background(), writeTempYAML(t, yaml))
	require.Error(t, err)
	assert.Contains(t, err.Error(), "context_budget_total must not be negative")
}

func TestParse_WorkflowContextBudgetTotal_OverrideSumExceeds(t *testing.T) {
	yaml := `
schema_version: "0.1"
workflow:
  id: "test-wf"
  name: "Override too big"
  context_budget_total: 1000
  steps:
    - id: "s1"
      agent: "planner"
      input: "a"
      context_budget: 800
    - id: "s2"
      agent: "planner"
      input: "b"
      context_budget: 800
`
	p := newTestPlanner()
	_, err := p.Parse(context.Background(), writeTempYAML(t, yaml))
	require.Error(t, err)
	assert.Contains(t, err.Error(), "sum of per-step context_budget overrides")
}

func TestParse_WorkflowContextBudgetTotal_OverrideSumEqualsTotal(t *testing.T) {
	yaml := `
schema_version: "0.1"
workflow:
  id: "test-wf"
  name: "Equal sum"
  context_budget_total: 1000
  steps:
    - id: "s1"
      agent: "planner"
      input: "a"
      context_budget: 600
    - id: "s2"
      agent: "planner"
      input: "b"
      context_budget: 400
`
	p := newTestPlanner()
	wf, err := p.Parse(context.Background(), writeTempYAML(t, yaml))
	require.NoError(t, err, "override sum equal to total is valid (zero remainder)")
	assert.Equal(t, 1000, wf.ContextBudgetTotal)
}

// RFC 0008 PR-1 review (H3): a per-step context_budget override only takes
// effect when the workflow opts into packaging via context_budget_total.
// Setting a per-step override with no workflow total is a footgun — the
// author intends packaging on, but the allocator returns nil and the agent
// gets legacy passthrough. Reject loudly instead of silently dropping.
func TestParse_StepContextBudgetWithoutWorkflowTotal_Rejected(t *testing.T) {
	yaml := `
schema_version: "0.1"
workflow:
  id: "test-wf"
  name: "Per-step only"
  steps:
    - id: "s1"
      agent: "planner"
      input: "a"
      context_budget: 16000
`
	p := newTestPlanner()
	_, err := p.Parse(context.Background(), writeTempYAML(t, yaml))
	require.Error(t, err)
	assert.Contains(t, err.Error(), `step "s1": context_budget set`)
	assert.Contains(t, err.Error(), "context_budget_total is unset")
}

// TestParse_AllOverridesEqualTotal_NonOverriddenStepRejected pins the M7
// planner-tighten disposition (RFC 0008 PR 6a): when per-step overrides
// exhaust the total but at least one step is non-overridden, the equal-
// split allocator hands that step zero tokens and `executeStep`'s
// `budget > 0` gate then skips packaging entirely — the author opted into
// packaging on the workflow yet a step silently gets legacy passthrough.
// The planner must reject the workflow at parse time.
func TestParse_AllOverridesEqualTotal_NonOverriddenStepRejected(t *testing.T) {
	yaml := `
schema_version: "0.1"
workflow:
  id: "test-wf"
  name: "Sum equals total but a non-overridden step exists"
  context_budget_total: 1000
  steps:
    - id: "s1"
      agent: "planner"
      input: "a"
      context_budget: 600
    - id: "s2"
      agent: "planner"
      input: "b"
      context_budget: 400
    - id: "s3"
      agent: "planner"
      input: "c"
`
	p := newTestPlanner()
	_, err := p.Parse(context.Background(), writeTempYAML(t, yaml))
	require.Error(t, err)
	assert.Contains(t, err.Error(), "non-overridden step")
	assert.Contains(t, err.Error(), ">= 1 token")
}
